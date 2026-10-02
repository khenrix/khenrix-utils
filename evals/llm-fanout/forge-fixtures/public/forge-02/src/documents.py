"""Public synthetic tenant-scoped title update, deliberately flawed."""
def update_title(db, tenant, identifier, title):
    cursor = db.execute('UPDATE documents SET title=? WHERE id=?', (title, identifier))
    return cursor.rowcount
