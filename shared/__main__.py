"""
python -m shared  →  directly launches the Dashboard.
"""
from shared.dashboard import Dashboard

if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()
