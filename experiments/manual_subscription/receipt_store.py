"""Bounded offline receipt quarantine. Files never imply bank verification."""
import hashlib
import secrets

from ledger import clock, reference

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 20
MAX_ORDER_FILES = 5
MIME = {'image/png': ('png', b'\x89PNG\r\n\x1a\n'),
        'image/jpeg': ('jpg', b'\xff\xd8\xff'),
        'application/pdf': ('pdf', b'%PDF-')}


class ReceiptStore:
    """Same transaction as the order transition; no filenames, paths or URLs.

    Signature sniffing is only a type check, NOT malware scanning, decoding or
    proof of genuine payment. Admin gets a download attachment, never inline HTML.
    Limits reject new files instead of deleting review/financial evidence.
    """
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.db:
            ledger.db.execute('''CREATE TABLE IF NOT EXISTS local_receipt_files (
                id TEXT PRIMARY KEY, order_id TEXT NOT NULL, uid INTEGER NOT NULL,
                mime TEXT NOT NULL, size INTEGER NOT NULL, digest TEXT NOT NULL,
                created INTEGER NOT NULL, data BLOB NOT NULL,
                UNIQUE(order_id,digest))''')

    def submit(self, uid, order_id, mime, data, now):
        clock(now)
        reference(order_id, 'order reference')
        if type(data) is not bytes or not 0 < len(data) <= MAX_FILE_BYTES:
            raise ValueError('Receipt file too large or empty')
        if mime not in MIME or not data.startswith(MIME[mime][1]):
            raise ValueError('Receipt format mismatch')
        digest = hashlib.sha256(data).hexdigest()
        db = self.ledger.db
        with db:
            db.execute('BEGIN IMMEDIATE')
            status = self.ledger.order_status(uid, order_id)
            same = db.execute('SELECT id FROM local_receipt_files WHERE order_id=? AND digest=?',
                              (order_id, digest)).fetchone()
            if status['state'] == 'review' and same and status['receipt'] == same[0]:
                return same[0]  # Lost HTTP response -> retry, never a new revision/file.
            if status['state'] not in ('awaiting', 'clarification') and not (status['state'] == 'review' and getattr(self.ledger, 'accepts_late_receipt', False)):
                raise ValueError('Order already under review or closed')
            if same:
                rid = same[0]
            else:
                count, size = db.execute('SELECT count(*),coalesce(sum(size),0) '
                                         'FROM local_receipt_files').fetchone()
                per_order = db.execute('SELECT count(*) FROM local_receipt_files WHERE order_id=?',
                                       (order_id,)).fetchone()[0]
                if count >= MAX_FILES or size+len(data) > MAX_TOTAL_BYTES or per_order >= MAX_ORDER_FILES:
                    raise ValueError('Receipt storage full')
                rid = 'RCPT-' + secrets.token_hex(16)
                db.execute('INSERT INTO local_receipt_files VALUES (?,?,?,?,?,?,?,?)',
                           (rid, order_id, uid, mime, len(data), digest, now, data))
            self.ledger._submit_receipt_locked(uid, order_id, rid, now)
        return rid

    def metadata(self, uid, order_id):
        status = self.ledger.order_status(uid, order_id)
        row = self.ledger.db.execute('SELECT id,mime,size,created FROM local_receipt_files '
                                     'WHERE id=? AND order_id=? AND uid=?',
                                     (status['receipt'], order_id, uid)).fetchone()
        return dict(zip(('id', 'mime', 'size', 'uploaded_at'), row)) if row else None

    def download(self, actor, receipt_id):
        self.ledger._admin(actor)
        reference(receipt_id, 'receipt reference')
        row = self.ledger.db.execute('SELECT mime,data FROM local_receipt_files WHERE id=?',
                                     (receipt_id,)).fetchone()
        if not row:
            raise ValueError('Unknown receipt file')
        return row[0], bytes(row[1])
