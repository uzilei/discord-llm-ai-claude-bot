import sqlite3

# program to quickly view everything in the memory
# ideally you'd use some other software or even the AI itself to view and edit
# but this is here for the lazy ones

conn = sqlite3.connect('memory.db')
rows = conn.execute('SELECT id, memory, timestamp FROM memories ORDER BY timestamp ASC').fetchall()
conn.close()

if not rows:
    print("No memories stored.")
else:
    for row in rows:
        print(f"[{row[0]}] {row[2][:16]} — {row[1]}")