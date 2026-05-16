
import tkinter as tk
from tkinter import ttk

root = tk.Tk()
root.title("Test Unicode")
try:
    label = tk.Label(root, text="Eye: \U0001F441", font=("Segoe UI", 20))
    label.pack(padx=20, pady=20)
except Exception as e:
    label = tk.Label(root, text=f"Error: {e}")
    label.pack()

def on_close():
    root.destroy()

root.after(2000, on_close)
root.mainloop()
