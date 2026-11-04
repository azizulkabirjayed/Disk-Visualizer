#ifndef _WIN32
#  define _POSIX_C_SOURCE 200809L
#  define _DEFAULT_SOURCE
#endif

#ifdef _WIN32
#  ifndef _WIN32_WINNT
#    define _WIN32_WINNT 0x0601          /* Windows 7+: FindExInfoBasic, SRWLOCK */
#  endif
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <windows.h>
#else
#  include <dirent.h>
#  include <errno.h>
#  include <fcntl.h>
#  include <pthread.h>
#  include <sys/stat.h>
#  include <sys/statvfs.h>
#  include <sys/types.h>
#  include <unistd.h>
#endif

#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <sqlite3.h>

#ifdef _WIN32
#  define DV_API __declspec(dllexport)
#else
#  define DV_API __attribute__((visibility("default")))
#endif

#define DV_VERSION 0x00030000   /* 3.0.0 — incremental refresh (dv_refresh, dv_find_path) */
#define DV_SCHEMA  3            /* bump when the SQLite layout changes (3: virtual "This PC" root) */
#define DV_MAX_DRIVES 32        /* per-drive "used bytes" slots kept in meta */

/* ------------------------------------------------------------------ public enums
 * gui.py keeps copies of these numbers — keep both sides in sync. */

enum {
    DV_OK             = 0,
    DV_ERR_BAD_INDEX  = 1,
    DV_ERR_BAD_BUF    = 2,
    DV_ERR_NOMEM      = 3,
    DV_ERR_OPEN_ROOT  = 4,
    DV_ERR_CANCELLED  = 5,
    DV_ERR_BUSY       = 6,
    DV_ERR_DB_OPEN    = 7,
    DV_ERR_DB_WRITE   = 8,
    DV_ERR_DB_CORRUPT = 9,
    DV_ERR_NO_DATA    = 10,
    DV_ERR_BAD_ARG    = 11,
    DV_ERR_NO_CHANGE  = 12   /* dv_refresh: nothing on disk changed, index kept as is */
};

enum { DV_PHASE_SCAN = 0, DV_PHASE_INDEX = 1, DV_PHASE_SAVE = 2, DV_PHASE_LOAD = 3,
       DV_PHASE_VERIFY = 4 };

enum {
    DV_CAT_FOLDER = 0, DV_CAT_IMAGE, DV_CAT_VIDEO, DV_CAT_AUDIO, DV_CAT_DOCUMENT,
    DV_CAT_ARCHIVE, DV_CAT_EXECUTABLE, DV_CAT_CODE, DV_CAT_SYSTEM, DV_CAT_OTHER,
    DV_CAT_COUNT
};

#define DV_FLAG_DENIED  0x01   /* folder could not be listed (permission/IO error) */
#define DV_FLAG_LINK    0x02   /* symlink / junction / mount point — not followed  */
#define DV_FLAG_MOUNT   0x04   /* another file system mounted here (POSIX) — not crossed */
#define DV_FLAG_VIRTUAL 0x08   /* the "This PC" root: not a real folder on disk    */
#define DV_FLAG_MASK    0x0F

/* ------------------------------------------------------------------ ABI structs
 * Mirrored in gui.py. Field order and types must match exactly; natural
 * alignment is used on both sides (ctypes follows the C compiler's rules). */

typedef struct {
    int64_t size;         /* bytes; folders: recursive total                */
    int64_t mtime;        /* last modified, Unix seconds (UTC)              */
    int64_t atime;        /* last opened (access time), Unix seconds        */
    int32_t parent;       /* parent index, -1 for the root                  */
    int32_t child_count;  /* direct children                                */
    int32_t rank;         /* position among siblings (largest first)        */
    int32_t file_count;   /* folders: files anywhere below                  */
    uint8_t is_dir;
    uint8_t category;     /* DV_CAT_*                                       */
    uint8_t flags;        /* DV_FLAG_*                                      */
    uint8_t reserved;
} dv_info;                /* 48 bytes */

typedef struct {
    double  x, y, w, h;   /* rectangle in the caller's coordinate space     */
    int64_t size;
    int32_t node;
    int16_t depth;        /* 0 = direct child of the layout root            */
    uint8_t is_dir;
    uint8_t category;
    uint8_t expanded;     /* 1 if this folder's children were laid out too  */
    uint8_t reserved[7];
} dv_rect;                /* 56 bytes */

typedef struct {
    int64_t files, dirs, bytes, skipped;  /* skipped = folders we could not list */
    int64_t scan_ms;                      /* duration of the crawl that produced the data */
    int64_t scanned_at;                   /* Unix seconds when that crawl finished */
    int32_t node_count;
    int32_t has_data;
} dv_stats;               /* 56 bytes */

/* Progress callback. Called on the thread that called dv_scan / dv_load_db /
 * dv_save_db (Python's QThread), throttled to roughly every 60 ms.
 *   phase  DV_PHASE_*
 *   files, dirs, bytes   running totals (scan phase)
 *   done, total          rows/nodes processed (index/save/load phases)
 *   path   folder currently being listed (UTF-8), "" otherwise           */
typedef void (*dv_progress_fn)(int phase, int64_t files, int64_t dirs, int64_t bytes,
                               int64_t done, int64_t total, const char *path);

/* ------------------------------------------------------------------ internal model */

typedef struct {
    int64_t  size;
    int64_t  mtime;
    int64_t  atime;
    uint32_t name_off;     /* offset of the NUL-terminated name in pool  */
    int32_t  parent;
    int32_t  first_child;  /* children are contiguous: [first, first+count) */
    int32_t  child_count;
    int32_t  rank;
    int32_t  file_count;
    uint16_t name_len;
    uint8_t  is_dir;
    uint8_t  cat;
    uint8_t  flags;
} dv_node;

/* Nodes are created breadth-first, which gives three cheap invariants:
 *   - a parent always has a smaller index than its children,
 *   - the children of a folder occupy one contiguous index range,
 *   - names are appended to the pool in index order (name_off increases).
 * kids[] is a permutation of that range sorted by size (largest first), so the
 * tree view and the treemap both get "biggest first" ordering for free. */
typedef struct {
    dv_node *nodes;
    int32_t  count, cap;
    char    *pool;          /* UTF-8 names, each NUL-terminated     */
    size_t   pool_len, pool_cap;
    char    *lower;         /* ASCII-lower-cased copy of pool       */
    int32_t *kids;
    int64_t  files, dirs, bytes, skipped;
    int64_t  scan_ms, scanned_at;
    int64_t  cat_bytes[DV_CAT_COUNT];
    int64_t  cat_count[DV_CAT_COUNT];
    /* Used bytes of each scanned drive at the time the index was built, in the
     * same order as the drive nodes (children of the virtual root). Saved in
     * meta as "used0".."used31" and used by dv_refresh as a sanity check: a
     * drive whose usage moved a lot is re-crawled in full instead of trusting
     * folder timestamps. 0 = unknown. */
    int64_t  drive_used[DV_MAX_DRIVES];
} dv_tree;

static dv_tree g_tree;      /* the live index the GUI queries      */
static int     g_has_data;
static char    g_err[1024]; /* detail text for the last failed job */

/* One lock guards g_tree for queries; jobs (scan/load/save) are serialised by
 * g_busy. A scan builds a private tree and swaps it in at the very end, so
 * the previous results stay valid if the scan fails or is cancelled. */
#ifdef _WIN32
static SRWLOCK g_lock = SRWLOCK_INIT;
#  define LOCK()   AcquireSRWLockExclusive(&g_lock)
#  define UNLOCK() ReleaseSRWLockExclusive(&g_lock)
#else
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
#  define LOCK()   pthread_mutex_lock(&g_lock)
#  define UNLOCK() pthread_mutex_unlock(&g_lock)
#endif

static volatile long g_busy;
static volatile long g_cancel;

static int job_enter(void)
{
#if defined(_MSC_VER)
    return InterlockedCompareExchange(&g_busy, 1, 0) == 0;
#else
    return __sync_bool_compare_and_swap(&g_busy, 0L, 1L);
#endif
}

static void job_leave(void)
{
#if defined(_MSC_VER)
    InterlockedExchange(&g_busy, 0);
#else
    __sync_lock_release(&g_busy);
#endif
}

#define VALID(i) (g_has_data && (i) >= 0 && (i) < g_tree.count)

/* ------------------------------------------------------------------ small helpers */

static void set_err(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_err, sizeof g_err, fmt, ap);
    va_end(ap);
}

static int64_t now_ms(void)
{
#ifdef _WIN32
    return (int64_t)GetTickCount64();
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
#endif
}

static char ascii_lower(char c) { return (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c; }

static int is_sep(char c) { return c == '/' || c == '\\'; }

/* Copy a string into a caller buffer, always NUL-terminating (truncates). */
static int copy_out(const char *src, size_t len, char *buf, int buf_len)
{
    if (buf == NULL || buf_len <= 0)
        return DV_ERR_BAD_BUF;
    if (len > (size_t)(buf_len - 1))
        len = (size_t)(buf_len - 1);
    memcpy(buf, src, len);
    buf[len] = '\0';
    return DV_OK;
}

typedef struct { char *p; size_t cap; } dv_buf;

static int buf_reserve(dv_buf *b, size_t need)
{
    if (need <= b->cap)
        return 1;
    size_t cap = b->cap ? b->cap : 512;
    while (cap < need)
        cap *= 2;
    char *p = (char *)realloc(b->p, cap);
    if (!p)
        return 0;
    b->p = p;
    b->cap = cap;
    return 1;
}

/* Progress throttling ------------------------------------------------------- */

typedef struct { dv_progress_fn cb; int64_t last_ms; } prog_t;

static void report(prog_t *p, int force, int phase, int64_t files, int64_t dirs,
                   int64_t bytes, int64_t done, int64_t total, const char *path)
{
    if (!p || !p->cb)
        return;
    int64_t t = now_ms();
    if (!force && t - p->last_ms < 60)
        return;
    p->last_ms = t;
    p->cb(phase, files, dirs, bytes, done, total, path ? path : "");
}

/* ------------------------------------------------------------------ categories */

typedef struct { const char *ext; uint8_t cat; } ext_map;

static ext_map EXTS[] = {
    /* images */
    {"jpg", DV_CAT_IMAGE}, {"jpeg", DV_CAT_IMAGE}, {"png", DV_CAT_IMAGE}, {"gif", DV_CAT_IMAGE},
    {"bmp", DV_CAT_IMAGE}, {"tif", DV_CAT_IMAGE}, {"tiff", DV_CAT_IMAGE}, {"webp", DV_CAT_IMAGE},
    {"heic", DV_CAT_IMAGE}, {"heif", DV_CAT_IMAGE}, {"raw", DV_CAT_IMAGE}, {"cr2", DV_CAT_IMAGE},
    {"nef", DV_CAT_IMAGE}, {"arw", DV_CAT_IMAGE}, {"dng", DV_CAT_IMAGE}, {"svg", DV_CAT_IMAGE},
    {"ico", DV_CAT_IMAGE}, {"psd", DV_CAT_IMAGE}, {"xcf", DV_CAT_IMAGE}, {"avif", DV_CAT_IMAGE},
    {"blend", DV_CAT_IMAGE}, {"exr", DV_CAT_IMAGE}, {"hdr", DV_CAT_IMAGE},
    /* video */
    {"mp4", DV_CAT_VIDEO}, {"mkv", DV_CAT_VIDEO}, {"avi", DV_CAT_VIDEO}, {"mov", DV_CAT_VIDEO},
    {"wmv", DV_CAT_VIDEO}, {"flv", DV_CAT_VIDEO}, {"webm", DV_CAT_VIDEO}, {"m4v", DV_CAT_VIDEO},
    {"mpg", DV_CAT_VIDEO}, {"mpeg", DV_CAT_VIDEO}, {"3gp", DV_CAT_VIDEO}, {"ts", DV_CAT_VIDEO},
    {"vob", DV_CAT_VIDEO}, {"m2ts", DV_CAT_VIDEO}, {"mts", DV_CAT_VIDEO},
    /* audio */
    {"mp3", DV_CAT_AUDIO}, {"wav", DV_CAT_AUDIO}, {"flac", DV_CAT_AUDIO}, {"aac", DV_CAT_AUDIO},
    {"ogg", DV_CAT_AUDIO}, {"m4a", DV_CAT_AUDIO}, {"wma", DV_CAT_AUDIO}, {"opus", DV_CAT_AUDIO},
    {"aiff", DV_CAT_AUDIO}, {"alac", DV_CAT_AUDIO}, {"mid", DV_CAT_AUDIO}, {"midi", DV_CAT_AUDIO},
    /* documents */
    {"pdf", DV_CAT_DOCUMENT}, {"doc", DV_CAT_DOCUMENT}, {"docx", DV_CAT_DOCUMENT},
    {"xls", DV_CAT_DOCUMENT}, {"xlsx", DV_CAT_DOCUMENT}, {"ppt", DV_CAT_DOCUMENT},
    {"pptx", DV_CAT_DOCUMENT}, {"odt", DV_CAT_DOCUMENT}, {"ods", DV_CAT_DOCUMENT},
    {"odp", DV_CAT_DOCUMENT}, {"txt", DV_CAT_DOCUMENT}, {"rtf", DV_CAT_DOCUMENT},
    {"md", DV_CAT_DOCUMENT}, {"csv", DV_CAT_DOCUMENT}, {"epub", DV_CAT_DOCUMENT},
    {"tex", DV_CAT_DOCUMENT}, {"pages", DV_CAT_DOCUMENT}, {"key", DV_CAT_DOCUMENT},
    /* archives & disk images */
    {"zip", DV_CAT_ARCHIVE}, {"rar", DV_CAT_ARCHIVE}, {"7z", DV_CAT_ARCHIVE}, {"tar", DV_CAT_ARCHIVE},
    {"gz", DV_CAT_ARCHIVE}, {"bz2", DV_CAT_ARCHIVE}, {"xz", DV_CAT_ARCHIVE}, {"tgz", DV_CAT_ARCHIVE},
    {"zst", DV_CAT_ARCHIVE}, {"cab", DV_CAT_ARCHIVE}, {"iso", DV_CAT_ARCHIVE}, {"img", DV_CAT_ARCHIVE},
    {"dmg", DV_CAT_ARCHIVE}, {"lz4", DV_CAT_ARCHIVE}, {"lzma", DV_CAT_ARCHIVE}, {"vhd", DV_CAT_ARCHIVE},
    {"vhdx", DV_CAT_ARCHIVE}, {"vmdk", DV_CAT_ARCHIVE}, {"wim", DV_CAT_ARCHIVE},
    /* executables & installers */
    {"exe", DV_CAT_EXECUTABLE}, {"dll", DV_CAT_EXECUTABLE}, {"msi", DV_CAT_EXECUTABLE},
    {"sys", DV_CAT_EXECUTABLE}, {"bat", DV_CAT_EXECUTABLE}, {"cmd", DV_CAT_EXECUTABLE},
    {"ps1", DV_CAT_EXECUTABLE}, {"com", DV_CAT_EXECUTABLE}, {"scr", DV_CAT_EXECUTABLE},
    {"so", DV_CAT_EXECUTABLE}, {"dylib", DV_CAT_EXECUTABLE}, {"apk", DV_CAT_EXECUTABLE},
    {"jar", DV_CAT_EXECUTABLE}, {"sh", DV_CAT_EXECUTABLE}, {"appx", DV_CAT_EXECUTABLE},
    {"msix", DV_CAT_EXECUTABLE}, {"deb", DV_CAT_EXECUTABLE}, {"rpm", DV_CAT_EXECUTABLE},
    {"appimage", DV_CAT_EXECUTABLE},
    /* source code & markup */
    {"c", DV_CAT_CODE}, {"h", DV_CAT_CODE}, {"cpp", DV_CAT_CODE}, {"hpp", DV_CAT_CODE},
    {"cc", DV_CAT_CODE}, {"cs", DV_CAT_CODE}, {"java", DV_CAT_CODE}, {"py", DV_CAT_CODE},
    {"js", DV_CAT_CODE}, {"jsx", DV_CAT_CODE}, {"tsx", DV_CAT_CODE}, {"go", DV_CAT_CODE},
    {"rs", DV_CAT_CODE}, {"rb", DV_CAT_CODE}, {"php", DV_CAT_CODE}, {"html", DV_CAT_CODE},
    {"htm", DV_CAT_CODE}, {"css", DV_CAT_CODE}, {"json", DV_CAT_CODE}, {"xml", DV_CAT_CODE},
    {"yml", DV_CAT_CODE}, {"yaml", DV_CAT_CODE}, {"sql", DV_CAT_CODE}, {"ipynb", DV_CAT_CODE},
    {"kt", DV_CAT_CODE}, {"swift", DV_CAT_CODE}, {"lua", DV_CAT_CODE}, {"dart", DV_CAT_CODE},
    {"vue", DV_CAT_CODE}, {"asm", DV_CAT_CODE}, {"s", DV_CAT_CODE}, {"y", DV_CAT_CODE},
    {"l", DV_CAT_CODE}, {"toml", DV_CAT_CODE},
    /* data, caches and system files */
    {"log", DV_CAT_SYSTEM}, {"tmp", DV_CAT_SYSTEM}, {"dat", DV_CAT_SYSTEM}, {"db", DV_CAT_SYSTEM},
    {"sqlite", DV_CAT_SYSTEM}, {"cache", DV_CAT_SYSTEM}, {"bak", DV_CAT_SYSTEM}, {"pak", DV_CAT_SYSTEM},
    {"lnk", DV_CAT_SYSTEM}, {"ini", DV_CAT_SYSTEM}, {"cfg", DV_CAT_SYSTEM}, {"pdb", DV_CAT_SYSTEM},
    {"obj", DV_CAT_SYSTEM}, {"o", DV_CAT_SYSTEM}, {"lib", DV_CAT_SYSTEM}, {"a", DV_CAT_SYSTEM},
    {"pyc", DV_CAT_SYSTEM}, {"class", DV_CAT_SYSTEM}, {"etl", DV_CAT_SYSTEM}, {"evtx", DV_CAT_SYSTEM},
    {"dmp", DV_CAT_SYSTEM}, {"mdmp", DV_CAT_SYSTEM}, {"pf", DV_CAT_SYSTEM}, {"cab_", DV_CAT_SYSTEM},
    {"ldb", DV_CAT_SYSTEM}, {"pck", DV_CAT_SYSTEM}, {"bin", DV_CAT_SYSTEM}, {"pt", DV_CAT_SYSTEM},
    {"pth", DV_CAT_SYSTEM}, {"onnx", DV_CAT_SYSTEM}, {"npy", DV_CAT_SYSTEM}, {"h5", DV_CAT_SYSTEM},
    {"safetensors", DV_CAT_SYSTEM}, {"parquet", DV_CAT_SYSTEM},
};
#define EXT_COUNT ((int)(sizeof EXTS / sizeof EXTS[0]))
static int g_ext_sorted;

static int ext_cmp(const void *a, const void *b)
{
    return strcmp(((const ext_map *)a)->ext, ((const ext_map *)b)->ext);
}

/* Called only from inside a job (g_busy held), so no race on g_ext_sorted. */
static void ensure_ext_sorted(void)
{
    if (!g_ext_sorted) {
        qsort(EXTS, EXT_COUNT, sizeof EXTS[0], ext_cmp);
        g_ext_sorted = 1;
    }
}

static uint8_t classify(const char *name, size_t len)
{
    size_t end = len;
    /* Up to three purely numeric suffixes are skipped, so versioned names
     * such as "libfoo.so.1.29" or "backup.tar.1" still classify by type. */
    for (int tries = 0; tries < 4; ++tries) {
        size_t dot = end;
        for (size_t i = end; i > 0; --i) {
            if (name[i - 1] == '.') { dot = i - 1; break; }
        }
        if (dot == end || dot == 0)         /* no extension, or ".hidden" */
            return DV_CAT_OTHER;
        size_t el = end - dot - 1;
        if (el == 0 || el > 15)
            return DV_CAT_OTHER;
        char ext[16];
        int digits = 1;
        for (size_t i = 0; i < el; ++i) {
            ext[i] = ascii_lower(name[dot + 1 + i]);
            if (ext[i] < '0' || ext[i] > '9')
                digits = 0;
        }
        ext[el] = '\0';
        if (digits && tries < 3) {
            end = dot;
            continue;
        }
        ext_map key = {ext, 0};
        const ext_map *m = (const ext_map *)bsearch(&key, EXTS, EXT_COUNT, sizeof EXTS[0], ext_cmp);
        return m ? m->cat : DV_CAT_OTHER;
    }
    return DV_CAT_OTHER;
}

/* ------------------------------------------------------------------ tree building */

static void tree_free(dv_tree *T)
{
    free(T->nodes);
    free(T->pool);
    free(T->lower);
    free(T->kids);
    memset(T, 0, sizeof *T);
}

/* Append one node; returns its index or -1 when out of memory. */
static int32_t tree_add(dv_tree *T, int32_t parent, const char *name, size_t len,
                        int is_dir, int64_t size, int64_t mtime, int64_t atime)
{
    if (len > 65535)
        len = 65535;
    if (T->count == T->cap) {
        if (T->cap > INT32_MAX / 2)
            return -1;
        int32_t cap = T->cap ? T->cap * 2 : 4096;
        dv_node *n = (dv_node *)realloc(T->nodes, (size_t)cap * sizeof *n);
        if (!n)
            return -1;
        T->nodes = n;
        T->cap = cap;
    }
    if (T->pool_len + len + 1 > T->pool_cap) {
        size_t cap = T->pool_cap ? T->pool_cap : (size_t)1 << 16;
        while (cap < T->pool_len + len + 1)
            cap *= 2;
        if (cap > (size_t)UINT32_MAX)          /* name_off is 32-bit */
            return -1;
        char *p = (char *)realloc(T->pool, cap);
        if (!p)
            return -1;
        T->pool = p;
        T->pool_cap = cap;
    }
    int32_t i = T->count++;
    dv_node *n = &T->nodes[i];
    n->size = size;
    n->mtime = mtime;
    n->atime = atime;
    n->name_off = (uint32_t)T->pool_len;
    n->name_len = (uint16_t)len;
    n->parent = parent;
    n->first_child = -1;
    n->child_count = 0;
    n->rank = 0;
    n->file_count = 0;
    n->is_dir = (uint8_t)(is_dir != 0);
    n->cat = is_dir ? DV_CAT_FOLDER : classify(name, len);
    n->flags = 0;
    memcpy(T->pool + T->pool_len, name, len);
    T->pool[T->pool_len + len] = '\0';
    T->pool_len += len + 1;
    return i;
}

static int is_virtual(const dv_tree *T, int32_t i)
{
    return (T->nodes[i].flags & DV_FLAG_VIRTUAL) != 0;
}

/* 1 if a path separator goes between node i's parent and node i. Drive roots
 * (children of the virtual root) already are full paths, and a parent whose
 * name ends in a separator ("C:\", "/") needs none either. */
static int need_sep(const dv_tree *T, int32_t i)
{
    int32_t p = T->nodes[i].parent;
    if (p < 0 || is_virtual(T, p))
        return 0;
    const dv_node *pn = &T->nodes[p];
    return !(pn->name_len && is_sep(T->pool[pn->name_off + pn->name_len - 1]));
}

/* Full UTF-8 path of node i, rebuilt by walking the parent chain. A drive
 * root's name is its full path, so the walk stops below the virtual root.
 * The virtual root's own "path" is just its label. Writes into b, sets *out_len. */
static int tree_path(const dv_tree *T, int32_t i, dv_buf *b, size_t *out_len)
{
    const dv_node *N = T->nodes;
    size_t len = 0;
    for (int32_t j = i; j >= 0; j = N[j].parent) {
        if (j != i && is_virtual(T, j))
            break;
        len += N[j].name_len;
        if (need_sep(T, j))
            len++;
    }
    if (!buf_reserve(b, len + 1))
        return DV_ERR_NOMEM;
    b->p[len] = '\0';
    size_t pos = len;
    for (int32_t j = i; j >= 0; j = N[j].parent) {
        if (j != i && is_virtual(T, j))
            break;
        pos -= N[j].name_len;
        memcpy(b->p + pos, T->pool + N[j].name_off, N[j].name_len);
        if (need_sep(T, j))
#ifdef _WIN32
            b->p[--pos] = '\\';
#else
            b->p[--pos] = '/';
#endif
    }
    *out_len = len;
    return DV_OK;
}

/* ------------------------------------------------------------------ refresh helpers
 *
 * dv_refresh rebuilds the index while reusing every part of the previous one
 * that provably did not change. The test is a folder's own mtime: creating,
 * deleting or renaming an entry inside a directory updates that directory's
 * own timestamp (NTFS and ext4 both do this), so when the stored mtime still
 * matches the live one, that folder's entry list is still correct and can be
 * copied from the old index instead of being enumerated again.
 *
 * What it does NOT mean is that the subtree below it is unchanged: a directory
 * timestamp does not propagate to its parent, so a file created three levels
 * down leaves every ancestor's mtime untouched. Every folder therefore still
 * has to be VISITED and have its own timestamp checked — what the check saves
 * is the enumeration, which is the expensive part. A folder with 50 000
 * entries costs one stat instead of 50 000 directory reads, and the walk ends
 * up costing roughly one stat per folder rather than one read per file.
 *
 * Folders reached through a reused entry list have no live timestamp yet (the
 * copied one is the stored one, which would always compare equal), so they are
 * stat'ed individually when the queue reaches them. Folders found by an actual
 * listing already carry a live timestamp from that listing and are not
 * stat'ed again.
 *
 * Editing a file in place touches neither its own parent's mtime nor any
 * ancestor's, so a reused entry list can still carry a stale file size. Three
 * guards limit that: a timestamp that cannot be read counts as "changed", a
 * folder we were not allowed to list last time is always retried, and a drive
 * whose used space moved by more than the tolerance below is re-crawled in
 * full. */

#define DV_USED_TOL_PCT   1
#define DV_USED_TOL_MIN   (200LL * 1024 * 1024)

/* Used bytes of the volume holding `path` (0 when it cannot be determined). */
#ifdef _WIN32
static int64_t disk_used(const char *path)
{
    wchar_t w[1024];
    if (MultiByteToWideChar(CP_UTF8, 0, path, -1, w, 1024) <= 0)
        return 0;
    ULARGE_INTEGER avail, total, freeb;
    if (!GetDiskFreeSpaceExW(w, &avail, &total, &freeb))
        return 0;
    if (total.QuadPart < freeb.QuadPart)
        return 0;
    return (int64_t)(total.QuadPart - freeb.QuadPart);
}
#else
static int64_t disk_used(const char *path)
{
    struct statvfs s;
    if (statvfs(path, &s) != 0)
        return 0;
    if (s.f_blocks < s.f_bfree)
        return 0;
    return (int64_t)(s.f_blocks - s.f_bfree) * (int64_t)s.f_frsize;
}
#endif

/* One entry per node of the tree being built, kept in lockstep with it: every
 * tree_add on the refresh path is followed by exactly one vec_push.
 *   old   matching node in the previous index, -1 = no counterpart
 *   live  1 when this node's mtime came from the file system during THIS run
 *         (a directory listing or root_check), 0 when it was copied from the
 *         old index and must be stat'ed before it can be compared. */
typedef struct { int32_t old; int32_t live; } dv_pair;
typedef struct { dv_pair *v; int32_t n, cap; } pairvec;

static int vec_push(pairvec *a, int32_t old, int32_t live)
{
    if (a->n == a->cap) {
        if (a->cap > INT32_MAX / 2)
            return 0;
        int32_t cap = a->cap ? a->cap * 2 : 4096;
        dv_pair *p = (dv_pair *)realloc(a->v, (size_t)cap * sizeof *p);
        if (!p)
            return 0;
        a->v = p;
        a->cap = cap;
    }
    a->v[a->n].old = old;
    a->v[a->n].live = live;
    a->n++;
    return 1;
}

static uint32_t name_hash(const char *s, size_t n)
{
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < n; ++i) {
        h ^= (uint8_t)ascii_lower(s[i]);
        h *= 16777619u;
    }
    return h;
}

/* Does node `idx` of tree T carry this name? Case-insensitive on Windows
 * (where the file system is), exact elsewhere. */
static int name_eq(const dv_tree *T, int32_t idx, const char *s, size_t n)
{
    const dv_node *d = &T->nodes[idx];
    if (d->name_len != n)
        return 0;
#ifdef _WIN32
    const char *a = T->lower + d->name_off;      /* already lower-cased */
    for (size_t i = 0; i < n; ++i)
        if (a[i] != ascii_lower(s[i]))
            return 0;
    return 1;
#else
    return memcmp(T->pool + d->name_off, s, n) == 0;
#endif
}

/* Open-addressing map over the sub-FOLDERS of one old directory, rebuilt for
 * each directory we re-list. Files need no lookup: their size and timestamps
 * are taken from the live listing anyway. */
typedef struct {
    const dv_tree *O;        /* the previous index                        */
    pairvec       *oldv;
    int32_t       *map;      /* old node indices, -1 = empty slot         */
    int32_t        map_cap;  /* allocated slots                           */
    int32_t        map_mask; /* active size - 1, 0 = map not in use       */
} refresh_ctx;

static int map_build(refresh_ctx *rx, int32_t old_dir)
{
    rx->map_mask = 0;
    if (old_dir < 0)
        return 1;
    const dv_node *d = &rx->O->nodes[old_dir];
    if (!d->is_dir || d->child_count <= 0)
        return 1;
    int64_t want = (int64_t)d->child_count * 2;
    int32_t cap = 16;
    while ((int64_t)cap < want && cap < (INT32_MAX / 2))
        cap <<= 1;
    if (cap > rx->map_cap) {
        int32_t *m = (int32_t *)realloc(rx->map, (size_t)cap * sizeof *m);
        if (!m)
            return 0;
        rx->map = m;
        rx->map_cap = cap;
    }
    for (int32_t k = 0; k < cap; ++k)
        rx->map[k] = -1;
    uint32_t mask = (uint32_t)(cap - 1);
    for (int32_t k = 0; k < d->child_count; ++k) {
        int32_t c = d->first_child + k;
        if (!rx->O->nodes[c].is_dir)
            continue;
        uint32_t h = name_hash(rx->O->pool + rx->O->nodes[c].name_off,
                               rx->O->nodes[c].name_len) & mask;
        while (rx->map[h] >= 0)
            h = (h + 1) & mask;
        rx->map[h] = c;
    }
    rx->map_mask = cap - 1;
    return 1;
}

static int32_t map_find(const refresh_ctx *rx, const char *name, size_t len)
{
    if (!rx || rx->map_mask == 0)
        return -1;
    uint32_t mask = (uint32_t)rx->map_mask;
    uint32_t h = name_hash(name, len) & mask;
    for (int32_t probe = 0; probe <= rx->map_mask; ++probe) {
        int32_t c = rx->map[h];
        if (c < 0)
            return -1;
        if (name_eq(rx->O, c, name, len))
            return c;
        h = (h + 1) & mask;
    }
    return -1;
}

/* Aggregate sizes bottom-up, sort children, build the search pool and totals. */
typedef struct { int64_t size; int32_t idx; } size_pair;

static int pair_cmp(const void *a, const void *b)
{
    const size_pair *x = (const size_pair *)a, *y = (const size_pair *)b;
    if (x->size != y->size)
        return x->size > y->size ? -1 : 1;            /* largest first   */
    return x->idx < y->idx ? -1 : (x->idx > y->idx);  /* stable on index */
}

static int finalize_tree(dv_tree *T, prog_t *pg)
{
    dv_node *N = T->nodes;
    int32_t n = T->count;
    report(pg, 1, DV_PHASE_INDEX, 0, 0, 0, 0, n, "");

    /* children always have larger indices than their parent → one reverse pass */
    for (int32_t i = n - 1; i > 0; --i) {
        dv_node *c = &N[i], *p = &N[c->parent];
        p->size += c->size;
        p->file_count += c->is_dir ? c->file_count : 1;
    }

    T->files = T->dirs = T->skipped = 0;
    memset(T->cat_bytes, 0, sizeof T->cat_bytes);
    memset(T->cat_count, 0, sizeof T->cat_count);
    int32_t max_children = 0;
    for (int32_t i = 0; i < n; ++i) {
        if (N[i].is_dir) {
            if (N[i].child_count > max_children)
                max_children = N[i].child_count;
            if (N[i].flags & DV_FLAG_VIRTUAL)
                continue;                      /* "This PC" is not a real folder */
            T->dirs++;
            T->cat_count[DV_CAT_FOLDER]++;
            if (N[i].flags & DV_FLAG_DENIED)
                T->skipped++;
        } else {
            T->files++;
            T->cat_bytes[N[i].cat] += N[i].size;
            T->cat_count[N[i].cat]++;
        }
    }
    T->bytes = n ? N[0].size : 0;

    T->kids = (int32_t *)malloc(sizeof(int32_t) * (size_t)(n ? n : 1));
    if (!T->kids)
        return DV_ERR_NOMEM;
    for (int32_t i = 0; i < n; ++i)
        T->kids[i] = i;

    if (max_children > 1) {
        size_pair *tmp = (size_pair *)malloc(sizeof(size_pair) * (size_t)max_children);
        if (!tmp)
            return DV_ERR_NOMEM;
        for (int32_t i = 0; i < n; ++i) {
            int32_t cc = N[i].child_count;
            if (!N[i].is_dir || cc < 2)
                continue;
            int32_t f = N[i].first_child;
            for (int32_t k = 0; k < cc; ++k) {
                tmp[k].idx = f + k;
                tmp[k].size = N[f + k].size;
            }
            qsort(tmp, (size_t)cc, sizeof *tmp, pair_cmp);
            for (int32_t k = 0; k < cc; ++k)
                T->kids[f + k] = tmp[k].idx;
        }
        free(tmp);
    }
    for (int32_t i = 0; i < n; ++i) {
        if (!N[i].is_dir)
            continue;
        for (int32_t k = 0; k < N[i].child_count; ++k)
            N[T->kids[N[i].first_child + k]].rank = k;
    }

    /* Lower-cased copy of the name pool: same offsets, so a hit at byte p in
     * `lower` maps straight back to a node via binary search on name_off. */
    T->lower = (char *)malloc(T->pool_len + 1);
    if (!T->lower)
        return DV_ERR_NOMEM;
    for (size_t i = 0; i < T->pool_len; ++i)
        T->lower[i] = ascii_lower(T->pool[i]);
    T->lower[T->pool_len] = '\0';

    report(pg, 1, DV_PHASE_INDEX, T->files, T->dirs, T->bytes, n, n, "");
    return DV_OK;
}

/* Swap a freshly built tree in; the old one is freed outside the lock. */
static void install_tree(dv_tree *T)
{
    dv_tree old;
    LOCK();
    old = g_tree;
    g_tree = *T;
    g_has_data = 1;
    UNLOCK();
    tree_free(&old);
    memset(T, 0, sizeof *T);
}

/* ------------------------------------------------------------------ crawler */

enum { SD_OK = 0, SD_DENIED, SD_FAIL, SD_NOMEM, SD_CANCEL };

typedef struct {
    prog_t  pg;
    int64_t files, dirs, bytes, entries;
    dv_buf  path;
    char    sys_err[256];     /* OS error text of the last failed listing */
#ifdef _WIN32
    wchar_t *wbuf;
    size_t   wcap;
#endif
} scan_ctx;

#ifdef _WIN32

static int64_t ft_to_unix(const FILETIME *ft)
{
    ULARGE_INTEGER u;
    u.LowPart = ft->dwLowDateTime;
    u.HighPart = ft->dwHighDateTime;
    if (u.QuadPart < 116444736000000000ULL)
        return 0;
    return (int64_t)((u.QuadPart - 116444736000000000ULL) / 10000000ULL);
}

static void win_err_text(DWORD e, char *buf, size_t n)
{
    DWORD k = FormatMessageA(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
                             NULL, e, 0, buf, (DWORD)n, NULL);
    if (!k) {
        snprintf(buf, n, "Windows error %lu", (unsigned long)e);
        return;
    }
    while (k > 0 && (buf[k - 1] == '\r' || buf[k - 1] == '\n' || buf[k - 1] == ' ' || buf[k - 1] == '.'))
        buf[--k] = '\0';
    snprintf(buf + k, n - k, " (error %lu)", (unsigned long)e);
}

/* UTF-8 path → UTF-16 with the \\?\ long-path prefix (paths > MAX_PATH work
 * without any registry or manifest setting). Optionally appends "\*". */
static const wchar_t *to_win_path(scan_ctx *sc, const char *u8, size_t len, int add_star)
{
    int already = len >= 4 && u8[0] == '\\' && u8[1] == '\\' && u8[2] == '?' && u8[3] == '\\';
    int unc = !already && len >= 2 && is_sep(u8[0]) && is_sep(u8[1]);
    const wchar_t *pre = already ? L"" : unc ? L"\\\\?\\UNC\\" : L"\\\\?\\";
    const char *src = unc ? u8 + 2 : u8;
    int slen = (int)(unc ? len - 2 : len);
    int wn = slen ? MultiByteToWideChar(CP_UTF8, 0, src, slen, NULL, 0) : 0;
    if (slen && wn <= 0)
        return NULL;
    size_t plen = wcslen(pre), need = plen + (size_t)wn + 3;
    if (need > sc->wcap) {
        size_t cap = sc->wcap ? sc->wcap : 1024;
        while (cap < need)
            cap *= 2;
        wchar_t *p = (wchar_t *)realloc(sc->wbuf, cap * sizeof(wchar_t));
        if (!p)
            return NULL;
        sc->wbuf = p;
        sc->wcap = cap;
    }
    wcscpy(sc->wbuf, pre);
    if (wn)
        MultiByteToWideChar(CP_UTF8, 0, src, slen, sc->wbuf + plen, wn);
    size_t pos = plen + (size_t)wn;
    for (size_t i = plen; i < pos; ++i)        /* \\?\ paths must use backslashes */
        if (sc->wbuf[i] == L'/')
            sc->wbuf[i] = L'\\';
    if (add_star) {
        if (pos == 0 || sc->wbuf[pos - 1] != L'\\')
            sc->wbuf[pos++] = L'\\';
        sc->wbuf[pos++] = L'*';
    }
    sc->wbuf[pos] = L'\0';
    return sc->wbuf;
}

static int root_check(scan_ctx *sc, const char *root, size_t len, int64_t *mtime)
{
    const wchar_t *w = to_win_path(sc, root, len, 0);
    if (!w)
        return SD_NOMEM;
    WIN32_FILE_ATTRIBUTE_DATA fa;
    if (GetFileAttributesExW(w, GetFileExInfoStandard, &fa)) {
        if (!(fa.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
            snprintf(sc->sys_err, sizeof sc->sys_err, "not a folder");
            return SD_FAIL;
        }
        *mtime = ft_to_unix(&fa.ftLastWriteTime);
    }
    return SD_OK;    /* if attributes failed (e.g. drive roots), listing decides */
}

/* `rx` is NULL for a plain scan. During a refresh it supplies the map of the
 * old sub-folders of this directory, so every folder we add can be paired with
 * its previous node and have its own timestamp compared later. */
static int scan_dir(dv_tree *T, int32_t di, const char *path, size_t plen, scan_ctx *sc,
                    refresh_ctx *rx)
{
    const wchar_t *w = to_win_path(sc, path, plen, 1);
    if (!w)
        return SD_NOMEM;
    WIN32_FIND_DATAW fd;
    /* FindExInfoBasic skips 8.3 names, LARGE_FETCH asks for bigger batches:
     * both noticeably speed up enumeration on NTFS. */
    HANDLE h = FindFirstFileExW(w, FindExInfoBasic, &fd, FindExSearchNameMatch, NULL,
                                FIND_FIRST_EX_LARGE_FETCH);
    if (h == INVALID_HANDLE_VALUE) {
        DWORD e = GetLastError();
        win_err_text(e, sc->sys_err, sizeof sc->sys_err);
        return e == ERROR_ACCESS_DENIED ? SD_DENIED : SD_FAIL;
    }
    do {
        const wchar_t *fn = fd.cFileName;
        if (fn[0] == L'.' && (fn[1] == 0 || (fn[1] == L'.' && fn[2] == 0)))
            continue;
        char name[1024];
        int n = WideCharToMultiByte(CP_UTF8, 0, fn, -1, name, (int)sizeof name, NULL, NULL);
        if (n <= 1)
            continue;
        int is_dir = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
        int64_t size = is_dir ? 0 : (int64_t)(((uint64_t)fd.nFileSizeHigh << 32) | fd.nFileSizeLow);
        int32_t ci = tree_add(T, di, name, (size_t)(n - 1), is_dir, size,
                              ft_to_unix(&fd.ftLastWriteTime), ft_to_unix(&fd.ftLastAccessTime));
        if (ci < 0) {
            FindClose(h);
            return SD_NOMEM;
        }
        if (rx && !vec_push(rx->oldv, is_dir ? map_find(rx, name, (size_t)(n - 1)) : -1, 1)) {
            FindClose(h);
            return SD_NOMEM;
        }
        /* Junctions, symlinks and mount points are listed but not followed —
         * following them would double-count data or loop forever (a volume
         * mounted into a folder is also scanned as its own drive). Cloud-file
         * placeholders (OneDrive) are also reparse points but are real folders,
         * so only these two tags are excluded. */
        if (is_dir && (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) &&
            (fd.dwReserved0 == IO_REPARSE_TAG_MOUNT_POINT || fd.dwReserved0 == IO_REPARSE_TAG_SYMLINK))
            T->nodes[ci].flags |= DV_FLAG_LINK;
        T->nodes[di].child_count++;
        if (is_dir) sc->dirs++; else { sc->files++; sc->bytes += size; }
        if ((++sc->entries & 255) == 0) {
            report(&sc->pg, 0, DV_PHASE_SCAN, sc->files, sc->dirs, sc->bytes, 0, 0, path);
            if (g_cancel) {
                FindClose(h);
                return SD_CANCEL;
            }
        }
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return SD_OK;
}

#else  /* POSIX ------------------------------------------------------------ */

static int root_check(scan_ctx *sc, const char *root, size_t len, int64_t *mtime)
{
    (void)len;
    struct stat st;
    if (stat(root, &st) != 0) {
        snprintf(sc->sys_err, sizeof sc->sys_err, "%s", strerror(errno));
        return errno == EACCES ? SD_DENIED : SD_FAIL;
    }
    if (!S_ISDIR(st.st_mode)) {
        snprintf(sc->sys_err, sizeof sc->sys_err, "not a folder");
        return SD_FAIL;
    }
    *mtime = (int64_t)st.st_mtime;
    return SD_OK;
}

static int scan_dir(dv_tree *T, int32_t di, const char *path, size_t plen, scan_ctx *sc,
                    refresh_ctx *rx)
{
    (void)plen;
    DIR *d = opendir(path);
    if (!d) {
        int e = errno;
        snprintf(sc->sys_err, sizeof sc->sys_err, "%s", strerror(e));
        return (e == EACCES || e == EPERM) ? SD_DENIED : SD_FAIL;
    }
    int dfd = dirfd(d);
    /* A sub-folder on a different device than the folder being listed is a
     * mount point (like `du -x`). Mounted drives are scanned as their own
     * drive entries, so they are not crossed here. */
    struct stat dst;
    int have_dev = fstat(dfd, &dst) == 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        const char *fn = e->d_name;
        if (fn[0] == '.' && (fn[1] == 0 || (fn[1] == '.' && fn[2] == 0)))
            continue;
        struct stat st;
        if (fstatat(dfd, fn, &st, AT_SYMLINK_NOFOLLOW) != 0)
            continue;                                   /* vanished / unreadable entry */
        int is_dir = S_ISDIR(st.st_mode), is_lnk = S_ISLNK(st.st_mode);
        if (!is_dir && !is_lnk && !S_ISREG(st.st_mode))
            continue;                                   /* sockets, devices, fifos */
        int64_t size = S_ISREG(st.st_mode) ? (int64_t)st.st_size : 0;
        int32_t ci = tree_add(T, di, fn, strlen(fn), is_dir, size,
                              (int64_t)st.st_mtime, (int64_t)st.st_atime);
        if (ci < 0) {
            closedir(d);
            return SD_NOMEM;
        }
        if (rx && !vec_push(rx->oldv, is_dir ? map_find(rx, fn, strlen(fn)) : -1, 1)) {
            closedir(d);
            return SD_NOMEM;
        }
        if (is_lnk)
            T->nodes[ci].flags |= DV_FLAG_LINK;
        else if (is_dir && have_dev && st.st_dev != dst.st_dev)
            T->nodes[ci].flags |= DV_FLAG_MOUNT;
        T->nodes[di].child_count++;
        if (is_dir) sc->dirs++; else { sc->files++; sc->bytes += size; }
        if ((++sc->entries & 255) == 0) {
            report(&sc->pg, 0, DV_PHASE_SCAN, sc->files, sc->dirs, sc->bytes, 0, 0, path);
            if (g_cancel) {
                closedir(d);
                return SD_CANCEL;
            }
        }
    }
    closedir(d);
    return SD_OK;
}

#endif

/* Breadth-first crawl of several drives under one virtual root.
 *   roots  '\n'-separated list of drive root paths ("C:\\\nD:\\", "/\n/home")
 *   label  name of the virtual root node ("This PC")
 * Node 0 = virtual root, nodes 1..k = the drives that could be opened; the
 * node array then doubles as the work queue. Drives that cannot be opened are
 * left out; the scan fails only when none of them can be opened. */
static int do_scan(dv_tree *T, const char *roots, const char *label, scan_ctx *sc)
{
    const char *lbl = (label && *label) ? label : "This PC";
    int64_t now = (int64_t)time(NULL);
    if (tree_add(T, -1, lbl, strlen(lbl), 1, 0, now, now) < 0)
        return DV_ERR_NOMEM;
    T->nodes[0].flags = DV_FLAG_VIRTUAL;
    T->nodes[0].first_child = 1;

    int added = 0, failed = 0;
    char first_err[600] = "";
    for (const char *p = roots; *p;) {
        const char *nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        const char *next = nl ? nl + 1 : p + len;
        while (len > 0 && (p[len - 1] == '\r' || p[len - 1] == ' '))
            len--;
        /* drop trailing separators but keep "/" and "C:\" */
        while (len > 1 && is_sep(p[len - 1]) && !(len == 3 && p[1] == ':'))
            len--;
        if (len > 0) {
            if (!buf_reserve(&sc->path, len + 1))
                return DV_ERR_NOMEM;
            memcpy(sc->path.p, p, len);
            sc->path.p[len] = '\0';
            int64_t mtime = 0;
            int r = root_check(sc, sc->path.p, len, &mtime);
            if (r == SD_NOMEM)
                return DV_ERR_NOMEM;
            if (r != SD_OK) {
                if (!failed++)
                    snprintf(first_err, sizeof first_err, "'%s': %s", sc->path.p, sc->sys_err);
            } else {
                if (tree_add(T, 0, sc->path.p, len, 1, 0, mtime, mtime) < 0)
                    return DV_ERR_NOMEM;
                T->nodes[0].child_count++;
                sc->dirs++;
                if (added < DV_MAX_DRIVES)
                    T->drive_used[added] = disk_used(sc->path.p);
                added++;
            }
        }
        p = next;
    }
    if (!added) {
        if (failed)
            set_err("No drive could be opened — %s", first_err);
        else
            set_err("No drives to scan");
        return DV_ERR_OPEN_ROOT;
    }

    for (int32_t i = 1; i < T->count; ++i) {
        if (!T->nodes[i].is_dir || (T->nodes[i].flags & (DV_FLAG_LINK | DV_FLAG_MOUNT)))
            continue;
        if (g_cancel) {
            set_err("Scan cancelled");
            return DV_ERR_CANCELLED;
        }
        size_t plen;
        if (tree_path(T, i, &sc->path, &plen) != DV_OK)
            return DV_ERR_NOMEM;
        T->nodes[i].first_child = T->count;
        int r = scan_dir(T, i, sc->path.p, plen, sc, NULL);
        switch (r) {
        case SD_OK:
            break;
        case SD_DENIED:
        case SD_FAIL:
            T->nodes[i].flags |= DV_FLAG_DENIED;  /* skip it, keep scanning */
            break;
        case SD_NOMEM:
            set_err("Out of memory after %lld entries", (long long)T->count);
            return DV_ERR_NOMEM;
        default:
            set_err("Scan cancelled");
            return DV_ERR_CANCELLED;
        }
        report(&sc->pg, 0, DV_PHASE_SCAN, sc->files, sc->dirs, sc->bytes, 0, 0, sc->path.p);
    }
    report(&sc->pg, 1, DV_PHASE_SCAN, sc->files, sc->dirs, sc->bytes, 0, 0, "");
    return DV_OK;
}

/* ------------------------------------------------------------------ refresh crawl */

/* Append the stored children of old node `oi` as the children of new node `ni`,
 * one level only. Folders are inserted with size 0 and files with their stored
 * size, exactly as db_load does, so finalize_tree recomputes sizes, file
 * counts, ranks, kids[] and the search pool afterwards. Copied sub-folders go
 * on the same breadth-first queue marked live = 0, so each is stat'ed and
 * judged on its own timestamp when the queue reaches it. */
static int copy_children(dv_tree *T, const dv_tree *O, int32_t ni, int32_t oi,
                         pairvec *oldv, scan_ctx *sc, int64_t done, int64_t total)
{
    const dv_node *on = &O->nodes[oi];
    int32_t first = on->first_child, cc = on->child_count;
    for (int32_t k = 0; k < cc; ++k) {
        int32_t c = first + k;
        const dv_node *cn = &O->nodes[c];
        int32_t ci = tree_add(T, ni, O->pool + cn->name_off, cn->name_len, cn->is_dir,
                              cn->is_dir ? 0 : cn->size, cn->mtime, cn->atime);
        if (ci < 0)
            return DV_ERR_NOMEM;
        T->nodes[ci].flags = (uint8_t)(cn->flags & ~DV_FLAG_VIRTUAL);
        if (!vec_push(oldv, c, 0))      /* stored mtime: needs a stat later */
            return DV_ERR_NOMEM;
        T->nodes[ni].child_count++;
        if (cn->is_dir) sc->dirs++; else { sc->files++; sc->bytes += cn->size; }
        if ((++sc->entries & 4095) == 0) {
            report(&sc->pg, 0, DV_PHASE_VERIFY, sc->files, sc->dirs, sc->bytes, done, total, "");
            if (g_cancel)
                return DV_ERR_CANCELLED;
        }
    }
    return DV_OK;
}

/* Rebuild the index from the previous one plus whatever actually changed.
 * Same breadth-first shape as do_scan — node 0 is the virtual root, nodes
 * 1..k are the drives, and the node array doubles as the work queue — except
 * that each queued folder carries the index of its counterpart in the old
 * tree, and is listed again only when its timestamp says it must be.
 * *changed_out is 0 when the result is identical to what we started from. */
static int do_refresh(dv_tree *T, const dv_tree *O, const char *roots, const char *label,
                      scan_ctx *sc, refresh_ctx *rx, int *changed_out)
{
    pairvec *oldv = rx->oldv;
    const char *lbl = (label && *label) ? label : "This PC";
    int64_t now = (int64_t)time(NULL);
    int changed = 0;

    if (tree_add(T, -1, lbl, strlen(lbl), 1, 0, now, now) < 0)
        return DV_ERR_NOMEM;
    T->nodes[0].flags = DV_FLAG_VIRTUAL;
    T->nodes[0].first_child = 1;
    if (!vec_push(oldv, 0, 1))
        return DV_ERR_NOMEM;

    const dv_node *oroot = &O->nodes[0];
    int32_t old_first = oroot->first_child, old_cc = oroot->child_count;
    if (!(oroot->flags & DV_FLAG_VIRTUAL))        /* index from another layout */
        return DV_ERR_NO_DATA;

    int added = 0, failed = 0, matched = 0;
    char first_err[600] = "";
    for (const char *p = roots; *p;) {
        const char *nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        const char *next = nl ? nl + 1 : p + len;
        while (len > 0 && (p[len - 1] == '\r' || p[len - 1] == ' '))
            len--;
        while (len > 1 && is_sep(p[len - 1]) && !(len == 3 && p[1] == ':'))
            len--;
        if (len > 0) {
            if (!buf_reserve(&sc->path, len + 1))
                return DV_ERR_NOMEM;
            memcpy(sc->path.p, p, len);
            sc->path.p[len] = '\0';
            int64_t mtime = 0;
            int r = root_check(sc, sc->path.p, len, &mtime);
            if (r == SD_NOMEM)
                return DV_ERR_NOMEM;
            if (r != SD_OK) {
                if (!failed++)
                    snprintf(first_err, sizeof first_err, "'%s': %s", sc->path.p, sc->sys_err);
            } else {
                int32_t od = -1;
                for (int32_t k = 0; k < old_cc; ++k) {
                    int32_t c = old_first + k;
                    if (name_eq(O, c, sc->path.p, len)) { od = c; break; }
                }
                int64_t cur_used = disk_used(sc->path.p);
                if (od >= 0) {
                    matched++;
                    int32_t slot = od - old_first;
                    int64_t prev = (slot >= 0 && slot < DV_MAX_DRIVES) ? O->drive_used[slot] : 0;
                    if (prev > 0 && cur_used > 0) {
                        int64_t diff = cur_used > prev ? cur_used - prev : prev - cur_used;
                        int64_t tol = prev / 100 * DV_USED_TOL_PCT;
                        if (tol < DV_USED_TOL_MIN)
                            tol = DV_USED_TOL_MIN;
                        if (diff > tol)
                            od = -1;            /* too much moved: crawl it in full */
                    } else if (prev <= 0 && cur_used > 0) {
                        changed = 1;            /* index predates the usage check */
                    }
                }
                if (tree_add(T, 0, sc->path.p, len, 1, 0, mtime, mtime) < 0)
                    return DV_ERR_NOMEM;
                if (!vec_push(oldv, od, 1))     /* root_check read its mtime */
                    return DV_ERR_NOMEM;
                T->nodes[0].child_count++;
                sc->dirs++;
                if (added < DV_MAX_DRIVES)
                    T->drive_used[added] = cur_used;
                added++;
                if (od < 0)
                    changed = 1;
            }
        }
        p = next;
    }
    if (!added) {
        if (failed)
            set_err("No drive could be opened — %s", first_err);
        else
            set_err("No drives to scan");
        return DV_ERR_OPEN_ROOT;
    }
    if (matched != old_cc)                       /* a drive disappeared */
        changed = 1;

    int64_t total = O->dirs > 0 ? O->dirs : 0, checked = 0;
    for (int32_t i = 1; i < T->count; ++i) {
        if (!T->nodes[i].is_dir || (T->nodes[i].flags & (DV_FLAG_LINK | DV_FLAG_MOUNT)))
            continue;
        if (g_cancel) {
            set_err("Refresh cancelled");
            return DV_ERR_CANCELLED;
        }
        int32_t od = oldv->v[i].old;
        int reuse = 0;
        if (od >= 0) {
            const dv_node *on = &O->nodes[od];
            if (!oldv->v[i].live) {
                /* Came from a reused entry list, so T's mtime is just a copy of
                 * the stored one. Ask the file system for the real one — one
                 * stat, instead of enumerating the folder. */
                size_t slen;
                if (tree_path(T, i, &sc->path, &slen) != DV_OK)
                    return DV_ERR_NOMEM;
                int64_t live_mtime = 0;
                int sr = root_check(sc, sc->path.p, slen, &live_mtime);
                if (sr == SD_NOMEM)
                    return DV_ERR_NOMEM;
                T->nodes[i].mtime = (sr == SD_OK) ? live_mtime : 0;
                oldv->v[i].live = 1;
            }
            /* An mtime of 0 means the timestamp could not be read (some volume
             * roots): treat it as "unknown", never as "unchanged". */
            if (on->is_dir && T->nodes[i].mtime > 0 && on->mtime == T->nodes[i].mtime &&
                !(on->flags & (DV_FLAG_DENIED | DV_FLAG_LINK | DV_FLAG_MOUNT)))
                reuse = 1;
        }
        T->nodes[i].first_child = T->count;
        if (reuse) {
            int rc = copy_children(T, O, i, od, oldv, sc, checked, total);
            if (rc != DV_OK) {
                if (rc == DV_ERR_NOMEM)
                    set_err("Out of memory after %lld entries", (long long)T->count);
                else
                    set_err("Refresh cancelled");
                return rc;
            }
        } else {
            changed = 1;
            T->nodes[i].flags &= (uint8_t)~DV_FLAG_DENIED;   /* give it another try */
            if (!map_build(rx, od)) {
                set_err("Out of memory while matching folders");
                return DV_ERR_NOMEM;
            }
            size_t plen;
            if (tree_path(T, i, &sc->path, &plen) != DV_OK)
                return DV_ERR_NOMEM;
            int r = scan_dir(T, i, sc->path.p, plen, sc, rx);
            switch (r) {
            case SD_OK:
                break;
            case SD_DENIED:
            case SD_FAIL:
                T->nodes[i].flags |= DV_FLAG_DENIED;
                break;
            case SD_NOMEM:
                set_err("Out of memory after %lld entries", (long long)T->count);
                return DV_ERR_NOMEM;
            default:
                set_err("Refresh cancelled");
                return DV_ERR_CANCELLED;
            }
            report(&sc->pg, 0, DV_PHASE_VERIFY, sc->files, sc->dirs, sc->bytes,
                   checked, total, sc->path.p);
        }
        checked++;
        if ((checked & 63) == 0)
            report(&sc->pg, 0, DV_PHASE_VERIFY, sc->files, sc->dirs, sc->bytes,
                   checked, total, "");
    }
    report(&sc->pg, 1, DV_PHASE_VERIFY, sc->files, sc->dirs, sc->bytes, checked, total, "");
    *changed_out = changed;
    return DV_OK;
}

/* ------------------------------------------------------------------ SQLite */

static const char *SCHEMA_SQL =
    "DROP TABLE IF EXISTS entries;"
    "CREATE TABLE entries("
    "  id       INTEGER PRIMARY KEY,"   /* == node index (breadth-first order) */
    "  parent   INTEGER NOT NULL,"      /* -1 for the root                    */
    "  name     TEXT    NOT NULL,"      /* root = label, drives = full path   */
    "  is_dir   INTEGER NOT NULL,"
    "  size     INTEGER NOT NULL,"      /* folders: recursive total           */
    "  mtime    INTEGER NOT NULL,"
    "  atime    INTEGER NOT NULL,"
    "  category INTEGER NOT NULL,"
    "  flags    INTEGER NOT NULL);"
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value);"
    "DELETE FROM meta;";

/* Map an SQLite result code onto ours. */
static int db_code(int src, int fallback)
{
    switch (src & 0xff) {
    case SQLITE_CORRUPT:
    case SQLITE_NOTADB:  return DV_ERR_DB_CORRUPT;
    case SQLITE_NOMEM:   return DV_ERR_NOMEM;
    default:             return fallback;
    }
}

static int db_exec(sqlite3 *db, const char *sql)
{
    char *em = NULL;
    int r = sqlite3_exec(db, sql, NULL, NULL, &em);
    if (r != SQLITE_OK)
        set_err("SQLite: %s", em ? em : sqlite3_errmsg(db));
    sqlite3_free(em);
    return r;
}

static int db_meta(sqlite3_stmt *st, const char *key, int64_t value)
{
    sqlite3_bind_text(st, 1, key, -1, SQLITE_STATIC);
    sqlite3_bind_int64(st, 2, value);
    int r = sqlite3_step(st);
    sqlite3_reset(st);
    return r == SQLITE_DONE ? SQLITE_OK : r;
}

static int db_write(const dv_tree *T, const char *path, prog_t *pg)
{
    sqlite3 *db = NULL;
    sqlite3_stmt *st = NULL;
    int rc = DV_ERR_DB_WRITE, src, in_tx = 0;

    src = sqlite3_open_v2(path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, NULL);
    if (src != SQLITE_OK) {
        set_err("Cannot open database '%s': %s", path, db ? sqlite3_errmsg(db) : "out of memory");
        sqlite3_close(db);
        return DV_ERR_DB_OPEN;
    }
    sqlite3_busy_timeout(db, 3000);

    /* WAL + synchronous=NORMAL: readers never block, and a bulk insert inside
     * a single transaction costs one fsync instead of one per row. */
    if ((src = db_exec(db, "PRAGMA journal_mode=WAL;")) != SQLITE_OK) goto fail;
    if ((src = db_exec(db, "PRAGMA synchronous=NORMAL; PRAGMA temp_store=MEMORY;"
                           "PRAGMA cache_size=-65536;")) != SQLITE_OK) goto fail;
    if ((src = db_exec(db, "BEGIN TRANSACTION;")) != SQLITE_OK) goto fail;
    in_tx = 1;
    if ((src = db_exec(db, SCHEMA_SQL)) != SQLITE_OK) goto fail;

    src = sqlite3_prepare_v2(db, "INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?)", -1, &st, NULL);
    if (src != SQLITE_OK) {
        set_err("SQLite: %s", sqlite3_errmsg(db));
        goto fail;
    }
    for (int32_t i = 0; i < T->count; ++i) {
        const dv_node *n = &T->nodes[i];
        sqlite3_bind_int64(st, 1, i);
        sqlite3_bind_int64(st, 2, n->parent);
        sqlite3_bind_text(st, 3, T->pool + n->name_off, n->name_len, SQLITE_STATIC);
        sqlite3_bind_int(st, 4, n->is_dir);
        sqlite3_bind_int64(st, 5, n->size);
        sqlite3_bind_int64(st, 6, n->mtime);
        sqlite3_bind_int64(st, 7, n->atime);
        sqlite3_bind_int(st, 8, n->cat);
        sqlite3_bind_int(st, 9, n->flags);
        if ((src = sqlite3_step(st)) != SQLITE_DONE) {
            set_err("SQLite insert failed: %s", sqlite3_errmsg(db));
            goto fail;
        }
        sqlite3_reset(st);
        if ((i & 4095) == 0) {
            report(pg, 0, DV_PHASE_SAVE, T->files, T->dirs, T->bytes, i, T->count, "");
            if (g_cancel) {
                set_err("Saving was cancelled — results are kept in memory only");
                src = SQLITE_OK;
                goto fail;
            }
        }
    }
    sqlite3_finalize(st);
    st = NULL;

    src = sqlite3_prepare_v2(db, "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", -1, &st, NULL);
    if (src != SQLITE_OK) {
        set_err("SQLite: %s", sqlite3_errmsg(db));
        goto fail;
    }
    if ((src = db_meta(st, "schema", DV_SCHEMA)) ||
        (src = db_meta(st, "files", T->files)) ||
        (src = db_meta(st, "dirs", T->dirs)) ||
        (src = db_meta(st, "bytes", T->bytes)) ||
        (src = db_meta(st, "skipped", T->skipped)) ||
        (src = db_meta(st, "scan_ms", T->scan_ms)) ||
        (src = db_meta(st, "scanned_at", T->scanned_at))) {
        set_err("SQLite: %s", sqlite3_errmsg(db));
        goto fail;
    }
    /* Used bytes per drive, so the next dv_refresh can tell whether trusting
     * folder timestamps is safe for that drive. */
    {
        int ndrv = (T->count > 0 && (T->nodes[0].flags & DV_FLAG_VIRTUAL))
                   ? T->nodes[0].child_count : 0;
        if (ndrv > DV_MAX_DRIVES)
            ndrv = DV_MAX_DRIVES;
        for (int k = 0; k < ndrv; ++k) {
            char key[24];
            snprintf(key, sizeof key, "used%d", k);
            if ((src = db_meta(st, key, T->drive_used[k])) != SQLITE_OK) {
                set_err("SQLite: %s", sqlite3_errmsg(db));
                goto fail;
            }
        }
    }
    sqlite3_finalize(st);
    st = NULL;

    if ((src = db_exec(db, "COMMIT;")) != SQLITE_OK) goto fail;
    in_tx = 0;
    db_exec(db, "PRAGMA wal_checkpoint(TRUNCATE);");   /* keep the -wal file small */
    report(pg, 1, DV_PHASE_SAVE, T->files, T->dirs, T->bytes, T->count, T->count, "");
    rc = DV_OK;

fail:
    if (rc != DV_OK)
        rc = db_code(src, DV_ERR_DB_WRITE);
    sqlite3_finalize(st);
    if (in_tx)
        sqlite3_exec(db, "ROLLBACK;", NULL, NULL, NULL);
    sqlite3_close(db);
    return rc;
}

static int corrupt(const char *what, long long at)
{
    set_err("Database is damaged: %s (row %lld)", what, at);
    return DV_ERR_DB_CORRUPT;
}

/* Read the whole entries table back into a RAM tree, validating every row
 * against the breadth-first invariants so a damaged file cannot produce an
 * inconsistent index (or out-of-range indices). */
static int db_load(dv_tree *T, const char *path, prog_t *pg)
{
    sqlite3 *db = NULL;
    sqlite3_stmt *st = NULL;
    int rc = DV_OK, src;
    long long n = 0, i = 0;

    src = sqlite3_open_v2(path, &db, SQLITE_OPEN_READWRITE, NULL);
    if (src != SQLITE_OK) {
        int cant = (src & 0xff) == SQLITE_CANTOPEN;
        set_err(cant ? "No saved scan yet" : "Cannot open database '%s': %s", path,
                db ? sqlite3_errmsg(db) : "out of memory");
        sqlite3_close(db);
        return cant ? DV_ERR_NO_DATA : DV_ERR_DB_OPEN;
    }
    sqlite3_busy_timeout(db, 3000);

    /* schema version — "no such table" simply means nothing was saved yet */
    src = sqlite3_prepare_v2(db, "SELECT value FROM meta WHERE key='schema'", -1, &st, NULL);
    if (src != SQLITE_OK) {
        rc = db_code(src, DV_ERR_NO_DATA);
        set_err(rc == DV_ERR_NO_DATA ? "No saved scan yet" : "Database is damaged: %s", sqlite3_errmsg(db));
        goto done;
    }
    src = sqlite3_step(st);
    if (src != SQLITE_ROW) {
        rc = src == SQLITE_DONE ? DV_ERR_NO_DATA : db_code(src, DV_ERR_DB_CORRUPT);
        set_err(rc == DV_ERR_NO_DATA ? "No saved scan yet" : "Database is damaged: %s", sqlite3_errmsg(db));
        goto done;
    }
    if (sqlite3_column_int(st, 0) != DV_SCHEMA) {
        /* older layout: behave like "nothing saved"; the next scan rewrites it */
        set_err("Saved scan is from an older version — scan again");
        rc = DV_ERR_NO_DATA;
        goto done;
    }
    sqlite3_finalize(st);
    st = NULL;

    /* scan timing metadata (counts are recomputed from the rows) */
    if (sqlite3_prepare_v2(db, "SELECT key, value FROM meta", -1, &st, NULL) == SQLITE_OK) {
        while (sqlite3_step(st) == SQLITE_ROW) {
            const char *k = (const char *)sqlite3_column_text(st, 0);
            if (!k) continue;
            if (!strcmp(k, "scan_ms"))         T->scan_ms = sqlite3_column_int64(st, 1);
            else if (!strcmp(k, "scanned_at")) T->scanned_at = sqlite3_column_int64(st, 1);
            else if (!strncmp(k, "used", 4) && k[4] >= '0' && k[4] <= '9') {
                int slot = atoi(k + 4);          /* per-drive used bytes */
                if (slot >= 0 && slot < DV_MAX_DRIVES)
                    T->drive_used[slot] = sqlite3_column_int64(st, 1);
            }
        }
    }
    sqlite3_finalize(st);
    st = NULL;

    src = sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM entries", -1, &st, NULL);
    if (src != SQLITE_OK || (src = sqlite3_step(st)) != SQLITE_ROW) {
        rc = db_code(src, DV_ERR_DB_CORRUPT);
        set_err("Database is damaged: %s", sqlite3_errmsg(db));
        goto done;
    }
    n = sqlite3_column_int64(st, 0);
    sqlite3_finalize(st);
    st = NULL;
    if (n <= 0) {
        set_err("No saved scan yet");
        rc = DV_ERR_NO_DATA;
        goto done;
    }
    if (n > INT32_MAX) {
        rc = corrupt("too many rows", n);
        goto done;
    }
    T->nodes = (dv_node *)malloc((size_t)n * sizeof(dv_node));
    if (!T->nodes) {
        set_err("Out of memory loading %lld entries", n);
        rc = DV_ERR_NOMEM;
        goto done;
    }
    T->cap = (int32_t)n;

    src = sqlite3_prepare_v2(db, "SELECT id, parent, name, is_dir, size, mtime, flags, atime "
                                 "FROM entries ORDER BY id", -1, &st, NULL);
    if (src != SQLITE_OK) {
        rc = db_code(src, DV_ERR_DB_CORRUPT);
        set_err("Database is damaged: %s", sqlite3_errmsg(db));
        goto done;
    }
    while ((src = sqlite3_step(st)) == SQLITE_ROW) {
        if (i >= n)                                       { rc = corrupt("row count changed", i); goto done; }
        if (sqlite3_column_int64(st, 0) != i)             { rc = corrupt("id sequence broken", i); goto done; }
        long long parent = sqlite3_column_int64(st, 1);
        const char *name = (const char *)sqlite3_column_text(st, 2);
        int nlen = sqlite3_column_bytes(st, 2);
        int is_dir = sqlite3_column_int(st, 3) != 0;
        int64_t size = sqlite3_column_int64(st, 4);
        int64_t mtime = sqlite3_column_int64(st, 5);
        int flags = sqlite3_column_int(st, 6) & DV_FLAG_MASK;
        int64_t atime = sqlite3_column_int64(st, 7);
        if (!name || nlen <= 0)                           { rc = corrupt("empty name", i); goto done; }
        if (i == 0) {
            if (parent != -1 || !is_dir)                  { rc = corrupt("bad root row", i); goto done; }
        } else {
            if (flags & DV_FLAG_VIRTUAL)                  { rc = corrupt("virtual node below the root", i); goto done; }
            if (parent < 0 || parent >= i)                { rc = corrupt("bad parent", i); goto done; }
            dv_node *p = &T->nodes[parent];
            if (!p->is_dir)                               { rc = corrupt("parent is a file", i); goto done; }
            if (p->first_child < 0)
                p->first_child = (int32_t)i;
            else if (p->first_child + p->child_count != i) { rc = corrupt("children not contiguous", i); goto done; }
            p->child_count++;
        }
        if (!is_dir && size < 0)                          { rc = corrupt("negative size", i); goto done; }
        int32_t ci = tree_add(T, (int32_t)parent, name, (size_t)nlen, is_dir, is_dir ? 0 : size, mtime, atime);
        if (ci < 0) {
            set_err("Out of memory loading %lld entries", n);
            rc = DV_ERR_NOMEM;
            goto done;
        }
        T->nodes[ci].flags = (uint8_t)flags;
        ++i;
        if ((i & 8191) == 0) {
            report(pg, 0, DV_PHASE_LOAD, 0, 0, 0, i, n, "");
            if (g_cancel) {
                set_err("Loading cancelled");
                rc = DV_ERR_CANCELLED;
                goto done;
            }
        }
    }
    if (src != SQLITE_DONE) {
        rc = db_code(src, DV_ERR_DB_CORRUPT);
        set_err("Database is damaged: %s", sqlite3_errmsg(db));
        goto done;
    }
    if (i != n)
        rc = corrupt("row count mismatch", i);
    else
        report(pg, 1, DV_PHASE_LOAD, 0, 0, 0, n, n, "");

done:
    sqlite3_finalize(st);
    sqlite3_close(db);
    return rc;
}

/* ------------------------------------------------------------------ jobs (exported) */

DV_API int dv_version(void) { return DV_VERSION; }

DV_API int dv_category_count(void) { return DV_CAT_COUNT; }

/* Lets gui.py confirm its ctypes.Structure mirrors match this build. */
DV_API int dv_abi_check(int info_size, int rect_size, int stats_size)
{
    return (info_size == (int)sizeof(dv_info) && rect_size == (int)sizeof(dv_rect) &&
            stats_size == (int)sizeof(dv_stats)) ? DV_OK : DV_ERR_BAD_ARG;
}

/* Request cancellation of the running job (safe to call from any thread). */
DV_API void dv_cancel(void) { g_cancel = 1; }

DV_API int dv_is_busy(void) { return g_busy != 0; }

/* The one body behind dv_scan() and dv_refresh(). Both jobs do exactly the
 * same things in the same order — take the job lock, walk the drives, index
 * what came back, swap it in and write it to the database — and differ only in
 * how the new tree is produced: a full scan builds it from nothing, a refresh
 * diffs it against the index already in RAM and may find that nothing moved at
 * all. That one difference is the `incremental` flag; everything around it used
 * to be copied out twice and now lives here once.
 *
 *   incremental = 0 → full scan (do_scan), 1 → update against g_tree (do_refresh)
 *
 * On a refresh with nothing changed the freshly built tree is thrown away and
 * the live index — and so every node index the caller holds — is left alone.
 * Only this function replaces or mutates g_tree, and the job lock keeps that
 * to one caller at a time. */
static int index_job(const char *roots, const char *label, const char *db_path,
                     dv_progress_fn cb, int incremental)
{
    if (!roots || !*roots)
        return DV_ERR_BAD_ARG;
    if (!job_enter())
        return DV_ERR_BUSY;
    if (incremental && !g_has_data) {      /* nothing to compare against */
        job_leave();
        return DV_ERR_NO_DATA;
    }
    g_cancel = 0;
    g_err[0] = '\0';
    ensure_ext_sorted();

    scan_ctx sc;
    memset(&sc, 0, sizeof sc);
    sc.pg.cb = cb;
    pairvec oldv;                          /* refresh only; zeroed, so the */
    memset(&oldv, 0, sizeof oldv);         /* frees below are no-ops on a scan */
    refresh_ctx rx;
    memset(&rx, 0, sizeof rx);
    rx.O = &g_tree;
    rx.oldv = &oldv;
    dv_tree T;
    memset(&T, 0, sizeof T);
    int changed = 1;                       /* a full scan is always a new tree */
    int64_t t0 = now_ms();

    int rc = incremental
           ? do_refresh(&T, &g_tree, roots, label, &sc, &rx, &changed)
           : do_scan(&T, roots, label, &sc);
    if (rc == DV_OK && changed) {
        T.scan_ms = now_ms() - t0;
        T.scanned_at = (int64_t)time(NULL);
        rc = finalize_tree(&T, &sc.pg);
        if (rc == DV_ERR_NOMEM)
            set_err("Out of memory while indexing %lld entries", (long long)T.count);
    }
    free(sc.path.p);
#ifdef _WIN32
    free(sc.wbuf);
#endif
    free(oldv.v);
    free(rx.map);
    if (rc != DV_OK) {
        tree_free(&T);
        job_leave();
        return rc;
    }
    if (!changed) {                        /* keep the live index as it is */
        tree_free(&T);
        job_leave();
        return DV_ERR_NO_CHANGE;
    }
    install_tree(&T);

    if (db_path && *db_path)
        rc = db_write(&g_tree, db_path, &sc.pg);
    job_leave();
    return rc;
}

/* Crawl every drive in `roots` ('\n'-separated) under one virtual root named
 * `label` ("This PC"), swap the result into RAM and save it to `db_path`
 * (NULL/"" = skip). Blocks until done — call it from a worker thread.
 * Returns DV_OK, or DV_ERR_DB_* when the scan succeeded but saving failed (the
 * new data is still live in RAM), or another error (old data kept). */
DV_API int dv_scan(const char *roots, const char *label, const char *db_path, dv_progress_fn cb)
{
    return index_job(roots, label, db_path, cb, 0);
}

/* Re-check the drives against the index already in RAM and rebuild it from
 * whatever actually changed, then save it to `db_path` (NULL/"" = skip).
 * Call it after a successful dv_load_db; it needs that index to compare with.
 *
 * Folders whose timestamp still matches are reused wholesale, so this is
 * typically seconds rather than minutes. Blocks until done — call it from a
 * worker thread.
 *
 * Returns DV_OK (the index was replaced; every node index the caller holds is
 * now stale), DV_ERR_NO_CHANGE (nothing moved, the live index was left exactly
 * as it was), DV_ERR_NO_DATA (nothing loaded to compare against — run a full
 * dv_scan instead), DV_ERR_DB_* when the rebuild worked but saving failed, or
 * another error, in which case the previous index is kept untouched. */
DV_API int dv_refresh(const char *roots, const char *label, const char *db_path, dv_progress_fn cb)
{
    return index_job(roots, label, db_path, cb, 1);
}

/* Load the last saved scan into RAM. DV_ERR_NO_DATA if nothing was saved. */
DV_API int dv_load_db(const char *db_path, dv_progress_fn cb)
{
    if (!db_path || !*db_path)
        return DV_ERR_BAD_ARG;
    if (!job_enter())
        return DV_ERR_BUSY;
    g_cancel = 0;
    g_err[0] = '\0';
    ensure_ext_sorted();

    prog_t pg = {cb, 0};
    dv_tree T;
    memset(&T, 0, sizeof T);
    int rc = db_load(&T, db_path, &pg);
    if (rc == DV_OK) {
        rc = finalize_tree(&T, &pg);
        if (rc == DV_ERR_NOMEM)
            set_err("Out of memory while indexing %lld entries", (long long)T.count);
    }
    if (rc == DV_OK)
        install_tree(&T);
    else
        tree_free(&T);
    job_leave();
    return rc;
}

/* Write the in-RAM index to `db_path` again (e.g. after a damaged file was deleted). */
DV_API int dv_save_db(const char *db_path, dv_progress_fn cb)
{
    if (!db_path || !*db_path)
        return DV_ERR_BAD_ARG;
    if (!job_enter())
        return DV_ERR_BUSY;
    g_cancel = 0;
    g_err[0] = '\0';
    prog_t pg = {cb, 0};
    int rc = g_has_data ? db_write(&g_tree, db_path, &pg) : DV_ERR_NO_DATA;
    job_leave();
    return rc;
}

/* ------------------------------------------------------------------ queries (exported) */

DV_API int dv_get_stats(dv_stats *out)
{
    if (!out)
        return DV_ERR_BAD_ARG;
    LOCK();
    memset(out, 0, sizeof *out);
    out->has_data = g_has_data;
    if (g_has_data) {
        out->files = g_tree.files;
        out->dirs = g_tree.dirs;
        out->bytes = g_tree.bytes;
        out->skipped = g_tree.skipped;
        out->scan_ms = g_tree.scan_ms;
        out->scanned_at = g_tree.scanned_at;
        out->node_count = g_tree.count;
    }
    UNLOCK();
    return DV_OK;
}

DV_API int dv_node_info(int32_t idx, dv_info *out)
{
    if (!out)
        return DV_ERR_BAD_ARG;
    LOCK();
    if (!VALID(idx)) {
        UNLOCK();
        return DV_ERR_BAD_INDEX;
    }
    const dv_node *n = &g_tree.nodes[idx];
    out->size = n->size;
    out->mtime = n->mtime;
    out->atime = n->atime;
    out->parent = n->parent;
    out->child_count = n->child_count;
    out->rank = n->rank;
    out->file_count = n->file_count;
    out->is_dir = n->is_dir;
    out->category = n->cat;
    out->flags = n->flags;
    out->reserved = 0;
    UNLOCK();
    return DV_OK;
}

/* Name only (root: its label, drives: the drive root path). */
DV_API int dv_node_name(int32_t idx, char *buf, int buf_len)
{
    LOCK();
    if (!VALID(idx)) {
        UNLOCK();
        return DV_ERR_BAD_INDEX;
    }
    const dv_node *n = &g_tree.nodes[idx];
    int rc = copy_out(g_tree.pool + n->name_off, n->name_len, buf, buf_len);
    UNLOCK();
    return rc;
}

/* Full path. Returns the path length in bytes (snprintf-style: if the return
 * value is >= buf_len the output was truncated — retry with a larger buffer),
 * or a negative DV_ERR_* code. */
DV_API int dv_node_path(int32_t idx, char *buf, int buf_len)
{
    if (!buf || buf_len <= 0)
        return -DV_ERR_BAD_BUF;
    LOCK();
    if (!VALID(idx)) {
        UNLOCK();
        return -DV_ERR_BAD_INDEX;
    }
    dv_buf b = {NULL, 0};
    size_t len = 0;
    int rc = tree_path(&g_tree, idx, &b, &len);
    UNLOCK();
    if (rc != DV_OK) {
        free(b.p);
        return -rc;
    }
    copy_out(b.p, len, buf, buf_len);
    free(b.p);
    return len > INT32_MAX ? INT32_MAX : (int)len;
}

/* Children of `idx`, largest first, starting at `offset`. Returns the number of
 * indices written to `out` (0 for files / past the end) or a negative error. */
DV_API int dv_children(int32_t idx, int32_t offset, int32_t *out, int32_t max_out)
{
    if (!out || max_out < 0 || offset < 0)
        return -DV_ERR_BAD_ARG;
    LOCK();
    if (!VALID(idx)) {
        UNLOCK();
        return -DV_ERR_BAD_INDEX;
    }
    const dv_node *n = &g_tree.nodes[idx];
    int32_t cnt = 0;
    if (n->is_dir && offset < n->child_count) {
        cnt = n->child_count - offset;
        if (cnt > max_out)
            cnt = max_out;
        memcpy(out, g_tree.kids + n->first_child + offset, (size_t)cnt * sizeof(int32_t));
    }
    UNLOCK();
    return cnt;
}

/* Is node i's name a leading component of `path`? Used to pick the drive a
 * path belongs to; a drive's name is its full root ("C:\", "/home"). */
static int path_prefix(const dv_tree *T, int32_t i, const char *path, size_t plen)
{
    const dv_node *d = &T->nodes[i];
    size_t nl = d->name_len;
    if (nl == 0 || nl > plen)
        return 0;
    const char *a = T->lower + d->name_off;
    for (size_t k = 0; k < nl; ++k) {
        char x = a[k], y = ascii_lower(path[k]);
        if (is_sep(x) && is_sep(y))
            continue;
        if (x != y)
            return 0;
    }
    if (nl == plen)
        return 1;
    return is_sep(path[nl]) || is_sep(T->pool[d->name_off + nl - 1]);
}

/* Index of the node for a full path, or a negative DV_ERR_* code when the path
 * is not in the current index. Lets the GUI hold on to a position across a
 * refresh, which invalidates every node index. */
DV_API int dv_find_path(const char *path)
{
    if (!path || !*path)
        return -DV_ERR_BAD_ARG;
    LOCK();
    if (!g_has_data) {
        UNLOCK();
        return -DV_ERR_NO_DATA;
    }
    const dv_tree *T = &g_tree;
    size_t plen = strlen(path);
    while (plen > 1 && is_sep(path[plen - 1]) && !(plen == 3 && path[1] == ':'))
        plen--;

    int32_t cur = -1;
    size_t pos = 0;
    if (name_eq(T, 0, path, plen)) {            /* the "This PC" label itself */
        UNLOCK();
        return 0;
    }
    const dv_node *r = &T->nodes[0];
    if (r->flags & DV_FLAG_VIRTUAL) {
        size_t best = 0;
        for (int32_t k = 0; k < r->child_count; ++k) {
            int32_t c = r->first_child + k;
            if (path_prefix(T, c, path, plen) && T->nodes[c].name_len > best) {
                best = T->nodes[c].name_len;
                cur = c;
            }
        }
        pos = best;
    } else if (path_prefix(T, 0, path, plen)) {
        cur = 0;
        pos = r->name_len;
    }

    while (cur >= 0 && pos < plen) {
        while (pos < plen && is_sep(path[pos]))
            pos++;
        size_t s = pos;
        while (pos < plen && !is_sep(path[pos]))
            pos++;
        if (pos == s)
            break;
        const dv_node *d = &T->nodes[cur];
        int32_t next = -1;
        if (d->is_dir) {
            for (int32_t k = 0; k < d->child_count; ++k) {
                int32_t c = d->first_child + k;
                if (name_eq(T, c, path + s, pos - s)) { next = c; break; }
            }
        }
        cur = next;
    }
    UNLOCK();
    return cur >= 0 ? cur : -DV_ERR_BAD_INDEX;
}

/* ---- search ---------------------------------------------------------------- */

/* min-heap keyed on size (ties: larger index counts as smaller) */
static int heap_less(const dv_node *N, int32_t a, int32_t b)
{
    if (N[a].size != N[b].size)
        return N[a].size < N[b].size;
    return a > b;
}

static void heap_down(const dv_node *N, int32_t *h, int n, int i)
{
    for (;;) {
        int l = 2 * i + 1, r = l + 1, m = i;
        if (l < n && heap_less(N, h[l], h[m])) m = l;
        if (r < n && heap_less(N, h[r], h[m])) m = r;
        if (m == i) return;
        int32_t t = h[i]; h[i] = h[m]; h[m] = t;
        i = m;
    }
}

static void heap_push(const dv_node *N, int32_t *h, int *n, int cap, int32_t v)
{
    if (cap <= 0)
        return;
    if (*n < cap) {
        int i = (*n)++;
        h[i] = v;
        while (i > 0) {
            int p = (i - 1) / 2;
            if (!heap_less(N, h[i], h[p])) break;
            int32_t t = h[i]; h[i] = h[p]; h[p] = t;
            i = p;
        }
    } else if (heap_less(N, h[0], v)) {
        h[0] = v;
        heap_down(N, h, *n, 0);
    }
}

static int passes(const dv_node *n, uint32_t mask, int64_t min_size, int kind)
{
    if (n->flags & DV_FLAG_VIRTUAL) return 0; /* never list "This PC" itself */
    if (kind == 1 && n->is_dir) return 0;     /* files only   */
    if (kind == 2 && !n->is_dir) return 0;    /* folders only */
    if (!(mask & (1u << n->cat))) return 0;
    return n->size >= min_size;
}

/* node whose name contains pool offset `off` (name_off is increasing) */
static int32_t node_at_offset(uint32_t off)
{
    int32_t lo = 0, hi = g_tree.count - 1;
    while (lo < hi) {
        int32_t mid = lo + (hi - lo + 1) / 2;
        if (g_tree.nodes[mid].name_off <= off) lo = mid; else hi = mid - 1;
    }
    return lo;
}

/* Case-insensitive (ASCII) substring search over every name in RAM.
 *   query      UTF-8; "" matches everything (filters only)
 *   cat_mask   bit (1 << DV_CAT_x) per allowed category
 *   min_size   bytes
 *   kind       0 any, 1 files only, 2 folders only
 * The largest max_out matches are written to `out`, biggest first; the total
 * number of matches goes to *total_out. Returns the count written, or < 0.
 *
 * Speed: rather than testing every name, we memchr/memcmp straight through the
 * lower-cased pool (one contiguous buffer) and map each hit back to its node
 * by binary search — typically well under 5 ms for a million names. */
DV_API int dv_search(const char *query, uint32_t cat_mask, int64_t min_size, int kind,
                     int32_t *out, int max_out, int64_t *total_out)
{
    if (!out || max_out < 0)
        return -DV_ERR_BAD_ARG;
    char q[512];
    size_t ql = query ? strlen(query) : 0;
    if (ql >= sizeof q)
        ql = sizeof q - 1;
    for (size_t i = 0; i < ql; ++i)
        q[i] = ascii_lower(query[i]);
    q[ql] = '\0';

    LOCK();
    if (!g_has_data) {
        UNLOCK();
        if (total_out) *total_out = 0;
        return 0;
    }
    const dv_node *N = g_tree.nodes;
    int hn = 0;
    int64_t total = 0;

    if (ql == 0) {
        for (int32_t i = 0; i < g_tree.count; ++i) {
            if (passes(&N[i], cat_mask, min_size, kind)) {
                total++;
                heap_push(N, out, &hn, max_out, i);
            }
        }
    } else {
        const char *base = g_tree.lower, *p = base, *end = base + g_tree.pool_len;
        while (p < end) {
            p = (const char *)memchr(p, q[0], (size_t)(end - p));
            if (!p || (size_t)(end - p) < ql)
                break;
            if (memcmp(p, q, ql) != 0) {
                ++p;
                continue;
            }
            /* the query holds no NUL, so a hit can never straddle two names */
            int32_t i = node_at_offset((uint32_t)(p - base));
            if (passes(&N[i], cat_mask, min_size, kind)) {
                total++;
                heap_push(N, out, &hn, max_out, i);
            }
            p = base + N[i].name_off + N[i].name_len + 1;   /* next name */
        }
    }
    /* heap-sort in place: popping the minimum to the back leaves `out`
     * ordered largest-first */
    for (int k = hn - 1; k > 0; --k) {
        int32_t t = out[0]; out[0] = out[k]; out[k] = t;
        heap_down(N, out, k, 0);
    }
    UNLOCK();
    if (total_out)
        *total_out = total;
    return hn;
}

/* ---- treemap --------------------------------------------------------------- */

typedef struct {
    const dv_tree *T;
    dv_rect *out;
    int      max, n, max_depth;
    double   min_side, header, pad;
} lay_ctx;

static void lay_dir(lay_ctx *c, int32_t dir, double x, double y, double w, double h, int depth);

static void lay_emit(lay_ctx *c, int32_t node, double x, double y, double w, double h, int depth)
{
    if (w < c->min_side || h < c->min_side || c->n >= c->max)
        return;                        /* too small to see: it still takes up space */
    const dv_node *nd = &c->T->nodes[node];
    int slot = c->n++;
    dv_rect *r = &c->out[slot];
    memset(r, 0, sizeof *r);
    r->x = x; r->y = y; r->w = w; r->h = h;
    r->size = nd->size;
    r->node = node;
    r->depth = (int16_t)depth;
    r->is_dir = nd->is_dir;
    r->category = nd->cat;
    if (nd->is_dir && nd->child_count > 0 && depth + 1 <= c->max_depth) {
        /* folders get a padding frame, plus a label strip when there is room */
        double hdr = (h >= c->header * 2.5 && w >= 48.0) ? c->header : 0.0;
        double ix = x + c->pad, iy = y + c->pad + hdr;
        double iw = w - 2 * c->pad, ih = h - 2 * c->pad - hdr;
        if (iw >= c->min_side && ih >= c->min_side)
            lay_dir(c, node, ix, iy, iw, ih, depth + 1);
        c->out[slot].expanded = (uint8_t)(c->n > slot + 1);
    }
}

/* worst aspect ratio of a row with total area s laid along a side of length L */
static double worst(double s, double amin, double amax, double side)
{
    double s2 = s * s, l2 = side * side;
    double a = l2 * amax / s2, b = s2 / (l2 * amin);
    return a > b ? a : b;
}

/* Squarified treemap (Bruls, Huizing, van Wijk 2000). Children arrive sorted
 * largest-first via kids[], which is exactly what the algorithm wants. */
static void lay_dir(lay_ctx *c, int32_t dir, double x, double y, double w, double h, int depth)
{
    const dv_node *N = c->T->nodes;
    const dv_node *d = &N[dir];
    if (d->child_count <= 0 || d->size <= 0 || w < c->min_side || h < c->min_side)
        return;
    const int32_t *kids = c->T->kids + d->first_child;
    int m = 0;
    double total = 0;
    for (int k = 0; k < d->child_count; ++k) {
        int64_t s = N[kids[k]].size;
        if (s <= 0) break;            /* sorted: everything after is empty too */
        total += (double)s;
        m++;
    }
    if (m == 0)
        return;
    double scale = (w * h) / total;
    int i = 0;
    while (i < m && c->n < c->max) {
        if (w < c->min_side * 0.5 || h < c->min_side * 0.5)
            break;
        double side = w < h ? w : h;
        double a0 = (double)N[kids[i]].size * scale;
        double rsum = a0, rmin = a0, rmax = a0;
        double cur = worst(rsum, rmin, rmax, side);
        int j = i + 1;
        while (j < m) {
            double a = (double)N[kids[j]].size * scale;
            double ns = rsum + a, nmin = rmin < a ? rmin : a, nmax = rmax > a ? rmax : a;
            double nw = worst(ns, nmin, nmax, side);
            if (nw > cur) break;
            rsum = ns; rmin = nmin; rmax = nmax; cur = nw;
            ++j;
        }
        if (w >= h) {                 /* lay the row as a column on the left */
            double tw = rsum / h, yy = y;
            for (int k = i; k < j; ++k) {
                double hh = (double)N[kids[k]].size * scale / tw;
                lay_emit(c, kids[k], x, yy, tw, hh, depth);
                yy += hh;
            }
            x += tw; w -= tw;
        } else {                      /* lay the row along the top */
            double th = rsum / w, xx = x;
            for (int k = i; k < j; ++k) {
                double ww = (double)N[kids[k]].size * scale / th;
                lay_emit(c, kids[k], xx, y, ww, th, depth);
                xx += ww;
            }
            y += th; h -= th;
        }
        i = j;
    }
}

/* Lay out the subtree under `root` inside (x, y, w, h). Rectangles are emitted
 * parent-before-children (depth-first), so for hit testing the last rectangle
 * containing a point is the deepest one. Returns the number written, or < 0.
 *   max_depth  how many folder levels to nest
 *   min_side   rectangles thinner than this are skipped
 *   header     label strip height for folders, pad = frame thickness */
DV_API int dv_treemap(int32_t root, double x, double y, double w, double h, int max_depth,
                      double min_side, double header, double pad, dv_rect *out, int max_out)
{
    if (!out || max_out <= 0 || w <= 0 || h <= 0)
        return -DV_ERR_BAD_ARG;
    LOCK();
    if (!VALID(root)) {
        UNLOCK();
        return -DV_ERR_BAD_INDEX;
    }
    lay_ctx c;
    c.T = &g_tree;
    c.out = out;
    c.max = max_out;
    c.n = 0;
    c.max_depth = max_depth < 0 ? 0 : max_depth;
    c.min_side = min_side < 0.5 ? 0.5 : min_side;
    c.header = header < 0 ? 0 : header;
    c.pad = pad < 0 ? 0 : pad;
    if (g_tree.nodes[root].is_dir)
        lay_dir(&c, root, x, y, w, h, 0);
    else
        lay_emit(&c, root, x, y, w, h, 0);
    UNLOCK();
    return c.n;
}

/* Bytes and item counts per category (index = DV_CAT_*; folders: count only). */
DV_API int dv_category_totals(int64_t *bytes, int64_t *counts, int n)
{
    if (n <= 0)
        return DV_ERR_BAD_ARG;
    if (n > DV_CAT_COUNT)
        n = DV_CAT_COUNT;
    LOCK();
    for (int i = 0; i < n; ++i) {
        if (bytes)  bytes[i]  = g_has_data ? g_tree.cat_bytes[i] : 0;
        if (counts) counts[i] = g_has_data ? g_tree.cat_count[i] : 0;
    }
    UNLOCK();
    return DV_OK;
}

/* ------------------------------------------------------------------ cleanup page (exported) */

/* Turn a min-heap built with heap_push() into a largest-first array (in place). */
static void heap_sort_desc(const dv_node *N, int32_t *h, int n)
{
    for (int k = n - 1; k > 0; --k) {
        int32_t t = h[0]; h[0] = h[k]; h[k] = t;
        heap_down(N, h, k, 0);
    }
}

/* Files not changed AND not opened since `cutoff` (Unix seconds).
 * "Last used" = the newer of mtime and atime, so an unreliable access time
 * (NTFS often stops updating it) can only make a file look newer, never older.
 * The largest max_out matches go to `out`, biggest first; *total_out / *bytes_out
 * receive the number and the combined size of ALL matches. */
DV_API int dv_find_old(int64_t cutoff, int64_t min_size, int32_t *out, int max_out,
                       int64_t *total_out, int64_t *bytes_out)
{
    if (!out || max_out < 0)
        return -DV_ERR_BAD_ARG;
    int hn = 0;
    int64_t total = 0, bytes = 0;
    LOCK();
    if (g_has_data) {
        const dv_node *N = g_tree.nodes;
        for (int32_t i = 1; i < g_tree.count; ++i) {
            const dv_node *n = &N[i];
            if (n->is_dir || n->size < min_size)
                continue;
            int64_t used = n->atime > n->mtime ? n->atime : n->mtime;
            if (used <= 0 || used >= cutoff)           /* unknown date, or used recently */
                continue;
            total++;
            bytes += n->size;
            heap_push(N, out, &hn, max_out, i);
        }
        heap_sort_desc(N, out, hn);
    }
    UNLOCK();
    if (total_out) *total_out = total;
    if (bytes_out) *bytes_out = bytes;
    return hn;
}

/* Folders whose name equals one of `names` ('|'-separated, ASCII case-insensitive),
 * e.g. "node_modules|__pycache__|Temp". A match nested inside another match is not
 * reported again (node_modules inside node_modules), so sizes are never counted
 * twice. Empty folders, links and the root itself are skipped. Output works
 * like dv_find_old: largest max_out matches, plus the count and size of all matches. */
DV_API int dv_find_named_dirs(const char *names, int32_t *out, int max_out,
                              int64_t *total_out, int64_t *bytes_out)
{
    if (!names || !out || max_out < 0)
        return -DV_ERR_BAD_ARG;

    char want[32][64];                /* lower-cased names to look for */
    size_t want_len[32];
    int nw = 0;
    for (const char *p = names; *p && nw < 32;) {
        const char *e = strchr(p, '|');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        if (len > 0 && len < sizeof want[0]) {
            for (size_t k = 0; k < len; ++k)
                want[nw][k] = ascii_lower(p[k]);
            want_len[nw++] = len;
        }
        p += len + (e ? 1 : 0);
    }

    int hn = 0;
    int64_t total = 0, bytes = 0;
    int rc = 0;
    LOCK();
    if (g_has_data && nw > 0) {
        const dv_node *N = g_tree.nodes;
        int32_t n = g_tree.count;
        /* covered[i] = node i is a match or lies inside one. Breadth-first order
         * means a parent is always decided before its children. */
        uint8_t *covered = (uint8_t *)calloc((size_t)n, 1);
        if (!covered) {
            rc = -DV_ERR_NOMEM;
        } else {
            for (int32_t i = 1; i < n; ++i) {
                const dv_node *d = &N[i];
                if (covered[d->parent]) {
                    covered[i] = 1;
                    continue;
                }
                if (!d->is_dir || (d->flags & (DV_FLAG_LINK | DV_FLAG_VIRTUAL)))
                    continue;
                const char *nm = g_tree.lower + d->name_off;
                for (int w = 0; w < nw; ++w) {
                    if (d->name_len == want_len[w] && memcmp(nm, want[w], want_len[w]) == 0) {
                        covered[i] = 1;
                        if (d->size > 0) {
                            total++;
                            bytes += d->size;
                            heap_push(N, out, &hn, max_out, i);
                        }
                        break;
                    }
                }
            }
            free(covered);
            heap_sort_desc(N, out, hn);
        }
    }
    UNLOCK();
    if (rc < 0)
        return rc;
    if (total_out) *total_out = total;
    if (bytes_out) *bytes_out = bytes;
    return hn;
}

/* ------------------------------------------------------------------ errors */

DV_API int dv_error_text(int code, char *buf, int buf_len)
{
    const char *msg;
    if (code < 0)
        code = -code;
    switch (code) {
    case DV_OK:             msg = "OK"; break;
    case DV_ERR_BAD_INDEX:  msg = "Item index out of range"; break;
    case DV_ERR_BAD_BUF:    msg = "Invalid output buffer"; break;
    case DV_ERR_NOMEM:      msg = "Out of memory"; break;
    case DV_ERR_OPEN_ROOT:  msg = "The drives cannot be read"; break;
    case DV_ERR_CANCELLED:  msg = "Cancelled"; break;
    case DV_ERR_BUSY:       msg = "Another scan is already running"; break;
    case DV_ERR_DB_OPEN:    msg = "The database cannot be opened"; break;
    case DV_ERR_DB_WRITE:   msg = "The database could not be written"; break;
    case DV_ERR_DB_CORRUPT: msg = "The database is damaged or incompatible"; break;
    case DV_ERR_NO_DATA:    msg = "No scan data"; break;
    case DV_ERR_BAD_ARG:    msg = "Invalid argument"; break;
    case DV_ERR_NO_CHANGE:  msg = "Nothing has changed"; break;
    default:                msg = "Unknown error"; break;
    }
    return copy_out(msg, strlen(msg), buf, buf_len);
}

/* Detail text (paths, OS / SQLite messages) for the last failed job. */
DV_API int dv_last_error(char *buf, int buf_len)
{
    return copy_out(g_err, strlen(g_err), buf, buf_len);
}
