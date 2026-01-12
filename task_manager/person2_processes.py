# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import time
import signal
import subprocess
from collections import deque, defaultdict
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

import psutil

from .config import DEFAULT_CFG, HISTORY_LEN, USER_AUTOSTART_DIR, SYS_AUTOSTART_DIRS, PROC_STATUS_LABEL
from .utils import fmt_bytes, safe_call, is_system_process, dt_from_ts, readlink_exe, run_cmd
from .models import ProcRow

class ProcessesTabMixin:

    def _build_processes_tab(self, parent):
        # Tao mot khung chua (Frame) gan vao cua so cha (Parent)
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=10, pady=8)

        # Tao o nhap lieu tim kiem Entry
        ttk.Label(top, text="Search:").pack(side="left")
        ent = ttk.Entry(top, textvariable=self.filter_text, width=35) # filter_text la bien de luu noi dung go
        ent.pack(side="left", padx=5)
        # Khi an phim Enter thi tai o nay goi ham refresh_processes
        ent.bind("<Return>", lambda e: self.refresh_processes(force=True))

        # Tao checkbox "Auto refresh" voi auto_refresh la bien Boolean
        ttk.Checkbutton(top, text="Auto refresh", variable=self.auto_refresh).pack(side="left", padx=10)
        ttk.Button(top, text="Refresh Now", command=lambda: self.refresh_all(force=True)).pack(side="left")

        #Tao mot khung con btns de chua cac nut hanh dong, day sang ben phai
        btns = ttk.Frame(top)
        btns.pack(side="right")
        # Cac nut chuc nang: End task, Kill, Properties...
        #Command=self.xxx: chi dinh ham se chay khi bam nut
        ttk.Button(btns, text="End task", command=self.end_task_sigterm).pack(side="right", padx=4)
        ttk.Button(btns, text="Kill (SIGKILL)", command=self.kill_process).pack(side="right", padx=4)
        ttk.Button(btns, text="Properties", command=self.proc_properties).pack(side="right", padx=4)
        ttk.Button(btns, text="Set priority", command=self.set_priority).pack(side="right", padx=4)

        # Dinh nghia danh sach cac cot (ID cua cot)
        cols = ("pid", "name", "user", "cpu", "mem", "status", "nice", "threads", "fds", "start", "cmd")
        # Tao bang Treeview, show="heading" nghia la chi hien tieu de cot, an cot goc (cot cay thu muc)
        self.proc_tree = ttk.Treeview(parent, columns=cols, show="headings", height=20)
        self.proc_tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Dictionary anh xa tu ID cot sang Ten hien thi 
        headings = {
            "pid": "PID", "name": "Name", "user": "User", "cpu": "CPU %",
            "mem": "Memory", "status": "Status", "nice": "Nice",
            "threads": "Threads", "fds": "FDs", "start": "Start time", "cmd": "Command",
        }

        # Vong lap thiet lap tung cot
        for c in cols:
            # Thiet lap tieu de cot va su kien Click vao tieu de -> goi ham _sort_processes
            self.proc_tree.heading(c, text=headings[c], command=lambda cc=c: self._sort_processes(cc))
            # Thiet lap width tuy theo noi dung cot
            w = 90
            if c in ("pid", "nice", "threads", "fds"): w = 70
            if c == "cpu": w = 80
            if c == "mem": w = 110
            if c in ("name", "user", "status"): w = 140
            if c == "start": w = 160
            if c == "cmd": w = 520
            # Can le van ban sang trai (West)
            self.proc_tree.column(c, width=w, anchor="w")

        # Goi ham an/ hien cot dua tren cau hinh (vi du: nguoi dung tat cot User thi an di)
        self._apply_process_columns_visibility()

        # Scrollbar
        ysb = ttk.Scrollbar(parent, orient="vertical", command=self.proc_tree.yview)
        # Ket noi 2 chieu: Scrollbar dieu khien Treeview, Treeview bao vi tri cho Scrollbar
        self.proc_tree.configure(yscrollcommand=ysb.set)
        ysb.place(in_=self.proc_tree, relx=1.0, rely=0, relheight=1.0, anchor="ne") # Dat nam de len mep phai bang

        # Menu chuot phai (Context menu)
        self.proc_menu = tk.Menu(self, tearoff=0) # tearoff=0 de ngan chan xuat hien cua so doc lap, hien thi luon tren cua so ung dung
        self.proc_menu.add_command(label="End task (SIGTERM)", command=self.end_task_sigterm)
        self.proc_menu.add_command(label="Kill (SIGKILL)", command=self.kill_process)
        self.proc_menu.add_separator()
        self.proc_menu.add_command(label="Set priority (nice)", command=self.set_priority)
        self.proc_menu.add_command(label="Set CPU affinity", command=self.set_affinity)
        self.proc_menu.add_separator()
        self.proc_menu.add_command(label="Properties", command=self.proc_properties)
        self.proc_menu.add_command(label="Open exe folder", command=self.open_exe_folder)

        # <Button-3>: Chuột phải -> Hiện menu
        self.proc_tree.bind("<Button-3>", self._popup_proc_menu)
        # <Double-1>: Click đúp chuột trái -> Xem thuộc tính
        self.proc_tree.bind("<Double-1>", lambda e: self.proc_properties())

    # Xu ly menu chuot phai
    def _popup_proc_menu(self, event):
        #identift_row(event.y): Tra ve ID cua dong (row) tai vi tri con tro chuot Y
        iid = self.proc_tree.identify_row(event.y)
        if iid:
            # Neu chuot dang tro vao 1 dong, thu hien boi den (select) dong do
            self.proc_tree.selection_set(iid)
            try:
                # Hien thi menu tai toa do chuot (x.root, y.root)
                self.proc_menu.tk_popup(event.x_root, event.y_root)
            finally:
                # Giai phong con tro de dong menu lai binh thuong
                self.proc_menu.grab_release()

    # Hàm này xử lý sự kiện UI khi click header
    def _sort_processes(self, col):
        if self.sort_col == col:
            # Nếu click lại vào cột đang sort -> đảo chiều (Tăng <-> Giảm)
            self.sort_desc = not self.sort_desc
        else:
            # Nếu click cột mới -> Mặc định giảm dần (hoặc tăng dần tùy logic bạn chọn)
            self.sort_col = col
            self.sort_desc = True
        # Gọi refresh để vẽ lại bảng theo thứ tự mới
        self.refresh_processes(force=True)


    def _apply_process_columns_visibility(self):
        cols = list(self.proc_tree["columns"])
        for c in cols:
            visible = bool(self.cfg["columns"].get(c, True))
            if visible:
                self.proc_tree.column(c, width=self.proc_tree.column(c, "width"), stretch=True)
            else:
                self.proc_tree.column(c, width=0, stretch=False)


    def _choose_columns_processes(self):
        self._choose_columns_dialog(title="Select columns (Processes)", cfg_key="columns",
                                    all_cols=list(self.proc_tree["columns"]),
                                    apply_cb=self._apply_process_columns_visibility)

    # Thu thap & loc du lieu
    def _collect_process_rows(self):
        rows = [] # danh sach chua ket qua

        # Lay text tim kiem, xoa khoang trang thua (.strip), chuyen thuong (.lower)
        search = self.filter_text.get().strip().lower()
        # Lay config xem co hien processes he thong khong
        show_system = bool(self.cfg.get("show_system_processes", True))

        # psutil.process.iter : ham cua thu vien tra ve iterator duyet qua toan bo process
        for p in psutil.process_iter():
            try:
                pid = p.pid
                name = p.name()
                # username(): Có thể gây lỗi nếu process đã chết hoặc không có quyền -> cần try/except bọc ngoài
                user = p.username() if hasattr(p, "username") else ""
                # Nếu không muốn hiện system process VÀ user này là system (root, daemon...) -> Bỏ qua
                if (not show_system) and is_system_process(user):
                    continue

                # filter (name/cmd/user/pid)
                cmdline = ""
                try:
                    cmdline = " ".join(p.cmdline()) if p.cmdline() else ""
                except Exception:
                    cmdline = ""
                # Logic Tìm kiếm: Ghép tất cả thông tin thành 1 chuỗi (hay) rồi tìm từ khóa (search)
                if search:
                    hay = f"{pid} {name} {user} {cmdline}".lower()
                    if search not in hay:
                        continue # Không tìm thấy -> Bỏ qua process này

                # cpu_percent(interval=None): Lấy % CPU dùng tức thời (non-blocking)
                cpu = 0.0
                try:
                    cpu = float(p.cpu_percent(interval=None) or 0.0)
                except Exception:
                    cpu = 0.0

                # memory_info().rss: Resident Set Size (RAM thực tế đang dùng, tính bằng Byte)
                mem_rss = 0
                try:
                    mem_rss = int(p.memory_info().rss)
                except Exception:
                    mem_rss = 0

                # ... (Lấy status, nice, threads tương tự, luôn dùng try/except để tránh crash)
                status = ""
                try:
                    st = p.status()
                    status = PROC_STATUS_LABEL.get(st, st)
                except Exception:
                    status = ""

                nice = 0
                try:
                    nice = int(p.nice())
                except Exception:
                    nice = 0

                threads = 0
                try:
                    threads = int(p.num_threads())
                except Exception:
                    threads = 0

                fds = 0
                if hasattr(p, "num_fds"):
                    try:
                        fds = int(p.num_fds())
                    except Exception:
                        fds = 0

                start_time = 0.0
                try:
                    start_time = float(p.create_time())
                except Exception:
                    start_time = 0.0

                # Dong goi du lieu/ Tạo object ProcRow (định nghĩa trong models.py) để lưu trữ gọn gàng
                rows.append(ProcRow(
                    pid=pid, name=name, user=user or "",
                    cpu=cpu, mem_rss=mem_rss, status=status, nice=nice,
                    threads=threads, fds=fds, start_time=start_time, cmd=cmdline
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # NoSuchProcess: Process vừa chạy xong đã tắt -> Bỏ qua
                # AccessDenied: Không có quyền truy cập (thường là process của Admin/Root) -> Bỏ qua
                continue
            except Exception:
                continue
        return rows

    # Hàm này cập nhật giao diện mà không làm "giật" màn hình (bằng cách so sánh cũ/mới).
    # Làm mới và hiển thị
    def refresh_processes(self, force=False):
        # Lấy dữ liệu mới nhất
        rows = self._collect_process_rows()
        # Sắp xếp dữ liệu (theo cột đang chọn, tăng hay giảm dần)
        rows = self._sort_rows(rows, self.sort_col, self.sort_desc)

        # Lấy danh sách ID (PID) đang hiển thị trên bảng
        existing = set(self.proc_tree.get_children(""))
        new_ids = set()

        for r in rows:
            iid = str(r.pid) # Dùng PID làm ID định danh cho dòng trong Treeview
            new_ids.add(iid)
            # Chuẩn bị dữ liệu hiển thị (Convert số sang chữ, format đẹp)
            values = (
                r.pid,
                r.name,
                r.user,
                f"{r.cpu:.1f}",
                fmt_bytes(r.mem_rss), # Hàm đổi byte -> MB/GB
                r.status,
                str(r.nice),
                str(r.threads),
                str(r.fds) if r.fds else "",
                dt_from_ts(r.start_time) if r.start_time else "",
                r.cmd
            )
            # Nếu dòng này đã có trên bảng -> Cập nhật giá trị (Update)
            if iid in existing:
                self.proc_tree.item(iid, values=values)
            else:
                # Nếu chưa có -> Chèn mới vào cuối (Insert)
                self.proc_tree.insert("", "end", iid=iid, values=values)
                
            self.proc_tree.move(iid, "", len(new_ids))

        #Những ID có trong 'existing' (cũ) mà không có trong 'new_ids' (mới)
        for iid in existing - new_ids:
            self.proc_tree.delete(iid)

    # Hàm này thưc hiện logic toán học để sort
    def _sort_rows(self, rows, col, desc: bool):
        reverse = bool(desc)
        # Map tên cột (UI) sang tên thuộc tính của object ProcRow (Code)
        colmap = {
            "pid": "pid", "name": "name", "user": "user", "cpu": "cpu", "mem": "mem_rss",
            "status": "status", "nice": "nice", "threads": "threads", "fds": "fds",
            "start": "start_time", "cmd": "cmd"
        }
        attr = colmap.get(col, col) # Lấy tên thuộc tính tương ứng
        def key_func(x):
            # Hàm lấy giá trị để so sánh. getattr(x, attr) tương đương x.attr
            v = getattr(x, attr, "")
            return v
        try:
            # sorted(): Hàm built-in của Python, trả về list đã sắp xếp
            return sorted(rows, key=key_func, reverse=reverse)
        except Exception:
            return rows

