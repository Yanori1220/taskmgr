#!/usr/bin/env python3
"""Task Manager in the terminal, laid out like the Windows one. Linux only.

    taskmgr                  refresh every second
    taskmgr --interval 2     refresh every 2 seconds
    taskmgr --lang zh        Chinese labels (not on the Linux console, Ctrl+Alt+F3: it has no CJK font)
    taskmgr --ascii          graphs in plain ASCII (automatic on the Linux console)

Inside, ? lists every key. Three tabs as in Windows: Processes (grouped by app, in three sections),
Performance (CPU, memory, each disk, each network adapter), Details (one row per process); and, with
an NVIDIA card, a fourth: GPU, every card side by side.
The Python programs of your own projects can be grouped by project: see groups.toml in the README.

Two things Linux keeps from an ordinary user, so this cannot show them either: how much network each
process uses (only the totals per adapter), and the disk I/O of other users' processes. Ending
another user's process needs sudo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import pwd
import re
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import psutil
from rich.console import Group as RichGroup
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (Button, DataTable, Footer, Input, ListItem, ListView, Static,
                             TabbedContent, TabPane)

try:
    import pynvml as nv
except ImportError:  # no NVIDIA library: the GPU parts are left out
    nv = None

try:
    import tomllib
except ModuleNotFoundError:  # before Python 3.11: groups.toml cannot be read
    tomllib = None


# ---------------------------------------------------------------- arguments, language, glyphs

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="taskmgr",
                                 description="Task Manager in the terminal, laid out like the Windows one.")
    ap.add_argument("--interval", type=float, default=1.0, metavar="SEC",
                    help="seconds between updates (default 1)")
    ap.add_argument("--lang", choices=("zh", "en"),
                    help="label language (default en)")
    ap.add_argument("--ascii", action="store_true",
                    help="draw graphs with ASCII only (default on the Linux console)")
    args = ap.parse_args(argv)
    on_console = os.environ.get("TERM") == "linux"  # Ctrl+Alt+F3: few block characters
    args.lang = args.lang or "en"
    args.ascii = args.ascii or on_console
    args.interval = max(0.25, args.interval)
    return args


# Parsed before the classes below are built, because their key bindings carry the labels.
ARGS = parse_args(None if __name__ == "__main__" else [])

HELP_ZH = """\
按鍵
  {tab_keys:<17}切換分頁：{tab_names}（也可以用滑鼠點）
  ↑ ↓  PgUp PgDn   移動
  Enter            展開或收合群組和分區（滑鼠：點一下選取，再點一下展開）
  /                搜尋名稱、PID 或命令列；Esc 清除
  s ／ r           換排序的欄位／反過來排（滑鼠：點欄位名稱）
  Del 或 k         結束工作（會先問）
  l                效能分頁的 CPU：整體／每個核心的工作狀態（使用率分成程式、系統核心、等待 I/O，
                   加上頻率，和最忙的核心上在跑的程式）
  z                效能和 GPU 分頁：圖涵蓋多長的時間：每格一次讀數／5 分鐘／15 分鐘
  空白鍵           暫停／繼續更新
  q 或 Ctrl+C      離開
  要選取畫面上的字：按住 Shift 再用滑鼠拖

欄位
  CPU          佔整台的百分比：{ncpu} 個邏輯處理器全滿是 100%，跟 Windows 一樣
  記憶體       程式自己用掉的記憶體，不算跟別的程式共用的檔案和共享記憶體
  磁碟         每秒實際讀寫硬碟的量
  GPU          這個程式在那張卡上的使用率
  GPU 記憶體   用了多少，後面是第幾張卡（編號跟 nvidia-smi 一樣）

分組
  像 Windows 一樣，同一個 app 的程式收成一組。vLLM 依 port 分組；PostgreSQL 一組；
  Linux 核心的執行緒一組。在終端機裡打的指令各自一組；終端機本身和它的 shell 在終端機那一組。
  自己專案的 Python 程式可以照專案分組，寫在 {config}（現在：{projects}）。

每個核心（效能分頁的 CPU 按 l）
  長條：藍色是程式，紅色是系統核心（中斷、驅動程式），灰色是等待 I/O；後面是使用率和現在的頻率。
  同一個實體核心的兩個執行緒排在一起；共用一塊 L3 快取的核心排成一欄（AMD 的晶片就是一塊 CCD）。
  下面的清單：最忙的邏輯處理器，和上面在跑的程式。只追蹤用量超過一個核心約 3% 的程式；
  程式在核心之間換來換去時，算在它最後停的那個核心上，所以有時候會是「—」。

GPU 分頁（按 4）
  使用率        NVIDIA 自己的定義：有任何工作在卡上跑的時間比例。一個小程式一直在跑就是 100%，
                所以看不出卡有多忙。
  SM 忙碌       卡上的運算單元（SM）有多少在工作
  張量核心      張量核心（專門做矩陣運算的部分）在跑的時間比例
  記憶體頻寬    卡的記憶體速度用了多少；LLM 產生文字時，速度多半卡在這裡
  PCIe          卡和電腦其他部分之間在搬的資料
  降頻原因      時脈現在為什麼沒到最高：功耗上限、過熱、閒置……
  SM 忙碌、張量核心、記憶體頻寬和 PCIe 要卡支援 NVIDIA 的 GPM 讀數（Hopper 以後的資料中心和
  工作站卡）；不支援的卡就不顯示這幾項。

Linux 的限制
  每個程式各用多少網路，一般使用者看不到，所以只有效能分頁的總量。
  別的使用者（root、postgres…）的程式看得到 CPU 和記憶體，看不到磁碟讀寫，
  要結束它得用 sudo。詳細資料分頁不列核心執行緒，它們都在「Linux 核心」那一組。

Esc 關閉"""

HELP_EN = """\
Keys
  {tab_keys:<17}switch tabs: {tab_names} (or click them)
  Up Down PgUp PgDn  move
  Enter            expand or collapse a group or section (mouse: click to select, click again to expand)
  /                search name, PID or command line; Esc clears
  s / r            next sort column / reverse it (mouse: click a column name)
  Del or k         end task (asks first)
  l                Performance tab, CPU: overall / what each core is doing (use split into programs,
                   kernel and I/O wait, its clock, and the programs on the busiest cores)
  z                Performance and GPU tabs: how much time the graphs cover: one reading to a
                   column / 5 minutes / 15 minutes
  Space            pause / resume updates
  q or Ctrl+C      quit
  To select text on screen, hold Shift while dragging

Columns
  CPU          share of the whole machine: all {ncpu} logical processors busy = 100%, as in Windows
  Memory       memory the process uses for itself, not counting shared files and shared memory
  Disk         bytes per second really read from and written to disk
  GPU          the process's utilization of the card it runs on
  GPU memory   how much it holds, and on which card (numbered as nvidia-smi does)

Groups
  As in Windows, the processes of one app are one group. vLLM is grouped by port; PostgreSQL is one;
  the Linux kernel's threads are one. Each command typed in a terminal is a group of its own; the
  terminal and its shells are one group. The Python programs of your own projects can be grouped
  by project in {config} (now: {projects}).

Each core (Performance tab, CPU, press l)
  The bar: blue is programs, red the kernel (interrupts, drivers), grey I/O wait; then the use and the clock.
  The two threads of one physical core sit together; the cores that share an L3 cache form a column
  (on AMD chips, one CCD).
  The list below: the busiest logical processors and the program on each. Only programs using over
  about 3% of one CPU are followed, and a program that moves between CPUs counts on the last one it
  ran on, so some rows show "—".

GPU tab (press 4)
  Utilization       NVIDIA's own figure: the share of time any work ran on the card; one small
                    program running all the time makes it 100%, so it says little about load.
  SMs busy          how many of the card's compute units (SMs) are at work
  Tensor cores      the share of time the tensor cores, the parts that do matrix math, ran
  Memory bandwidth  how much of the card's memory speed is in use; an LLM writing text is mostly
                    held back by this
  PCIe              data moving between the card and the rest of the computer
  Slowed by         why the clock is below its top right now: power cap, heat, idle...
  SMs busy, tensor cores, memory bandwidth and PCIe need a card with NVIDIA's GPM readings
  (data-center and workstation cards from Hopper on); other cards leave them out.

What Linux does not show
  How much network each process uses is hidden from an ordinary user, so only the totals are shown.
  Other users' processes (root, postgres...) show CPU and memory but not disk I/O, and ending
  them needs sudo. The Details tab leaves kernel threads out: they are all in the "Linux kernel" group.

Esc closes"""

STRINGS: dict[str, dict[str, str]] = {
    "zh": {
        "title": "工作管理員", "paused": "已暫停　空白鍵繼續",
        "tab_proc": "1 處理程序", "tab_perf": "2 效能", "tab_details": "3 詳細資料", "tab_gpu": "4 GPU",
        "sec_apps": "應用程式", "sec_background": "背景處理程序", "sec_system": "系統處理程序",
        "c_name": "名稱", "c_cpu": "CPU", "c_mem": "記憶體", "c_disk": "磁碟", "c_gpu": "GPU",
        "c_gpumem": "GPU 記憶體", "c_pid": "PID", "c_status": "狀態", "c_user": "使用者名稱",
        "c_threads": "執行緒", "c_cmd": "命令列",
        "st_running": "執行中", "st_sleeping": "休眠", "st_disk-sleep": "等待 I/O", "st_stopped": "已停止",
        "st_tracing-stop": "已停止", "st_zombie": "殭屍", "st_dead": "已結束", "st_idle": "閒置",
        "st_parked": "休眠", "st_waking": "喚醒中", "st_wake-kill": "喚醒中",
        "kernel": "Linux 核心",
        "search_ph": "搜尋名稱、PID 或命令列　Enter 回到列表　Esc 清除",
        "b_search": "搜尋", "b_end": "結束工作", "b_sort": "排序", "b_reverse": "反向",
        "b_cores": "每個核心", "b_pause": "暫停", "b_help": "說明", "b_quit": "離開", "k_space": "空白",
        "cpu": "CPU", "memory": "記憶體", "disk_n": "磁碟 {n}", "ethernet": "乙太網路", "wifi": "Wi-Fi",
        "gpu_n": "GPU {n}",
        "g_util": "% 使用率", "g_mem": "記憶體使用量", "g_active": "作用時間 %", "g_read": "讀取速度",
        "g_write": "寫入速度", "g_recv": "接收", "g_sent": "傳送", "g_gpumem": "專用 GPU 記憶體使用量",
        "g_transfer": "傳輸速度", "g_throughput": "傳輸量", "lg_read": "讀取", "lg_write": "寫入", "lg_recv": "接收",
        "lg_send": "傳送",
        "seconds": "{n} 秒",
        "i_util": "使用率", "i_speed": "速度", "i_maxspeed": "最高速度", "i_procs": "處理程序",
        "i_threads": "執行緒", "i_uptime": "開機時間", "i_cores": "核心", "i_logical": "邏輯處理器",
        "i_load": "負載平均", "i_inuse": "使用中", "i_avail": "可用", "i_cached": "快取", "i_shared": "共享",
        "i_swap": "置換", "i_total": "總共", "i_comp": "記憶體組成", "i_free": "閒置",
        "i_active": "作用時間", "i_resp": "平均回應時間", "i_read": "讀取速度", "i_write": "寫入速度",
        "i_capacity": "容量", "i_type": "類型", "i_model": "型號", "i_mounts": "掛載點",
        "i_space": "空間", "i_health": "健康度（udisks，每分鐘讀一次）", "h_used": "壽命用掉", "h_spare": "備用區",
        "h_written": "總寫入", "h_read": "總讀取", "h_hours": "開機時數", "h_unsafe": "異常斷電", "h_media": "媒體錯誤",
        "h_warn": "警告", "h_none": "沒有", "h_na": "讀不到健康度（udisks 沒有回應）",
        "i_recv": "接收", "i_sent": "傳送", "i_link": "連線速度", "i_adapter": "介面卡",
        "i_ipv4": "IPv4 位址", "i_ipv6": "IPv6 位址", "i_total_recv": "總共接收", "i_total_sent": "總共傳送",
        "i_down": "未連線", "i_vram": "記憶體", "i_temp": "溫度", "i_power": "功耗",
        "i_fan": "風扇", "i_sm": "核心時脈", "i_memclk": "記憶體時脈", "i_driver": "驅動程式版本",
        "i_gpu_procs": "使用這張卡的程式", "i_none": "沒有",
        "i_sm_busy": "SM 忙碌", "i_tensor": "張量核心", "i_bw": "記憶體頻寬", "i_pcie_tx": "PCIe 傳送",
        "i_pcie_rx": "PCIe 接收", "i_slowed": "降頻原因", "g_bw": "% 記憶體頻寬",
        "sl_idle": "閒置", "sl_power": "功耗上限", "sl_heat": "過熱", "sl_hw": "硬體降速", "sl_brake": "電源煞車",
        "sl_clocks": "時脈設定", "sl_display": "顯示時脈", "sl_sync": "同步加速", "sl_board": "板卡限制",
        "sl_reliability": "可靠性",
        "b_zoom": "時間範圍", "minutes": "{n} 分鐘",
        "cores_hint": "l：看每個核心的工作狀態", "cores_back": "l：回到整體",
        "lg_user": "程式", "lg_sys": "系統核心", "lg_iow": "等待 I/O", "lg_cell": "每格：邏輯處理器　使用率　頻率 GHz",
        "lg_pairs": "同一個實體核心的兩個執行緒排在一起：{a} 和 {b}、{c} 和 {d}……",
        "busy_title": "最忙的邏輯處理器，和上面在跑的程式", "busy_none": "現在沒有忙的核心（都在 5% 以下）",
        "busy_kernel": "系統核心（中斷、驅動程式）", "busy_note": "「—」：認不出是哪個程式（很小的程式不追蹤；換過核心的只算在最後那個，見 ?）",
        "i_cpu_temp": "溫度",
        "end_title": "結束「{name}」？", "end_count": "{n} 個程式：", "end_more": "……還有 {n} 個",
        "end_explain": "結束：送 SIGTERM，讓它自己收尾再結束。\n強制結束：送 SIGKILL，立刻停掉，來不及收尾。",
        "end_others": "其中 {n} 個屬於 {users}，要 sudo 才結束得了。",
        "btn_end": "結束 Enter", "btn_kill": "強制結束 F", "btn_cancel": "取消 Esc",
        "sent": "已送出 {sig} 給 {n} 個程式", "denied": "{n} 個沒有權限：{users} 的程式要用 sudo 結束",
        "gone": "{n} 個已經不在了", "no_kernel": "Linux 核心的執行緒不能結束", "no_init": "PID 1 不能結束",
        "no_self": "這是工作管理員自己，要離開按 q", "nothing": "這一列沒有可以結束的程式",
        "sample_error": "讀取失敗：{err}", "help": HELP_ZH,
        "cfg_error": "讀不了 {path}：{err}　專案分組先關掉", "cfg_none": "還沒設定",
        "cfg_python": "要 Python 3.11 以上才讀得了",
    },
    "en": {
        "title": "Task Manager", "paused": "Paused   Space resumes",
        "tab_proc": "1 Processes", "tab_perf": "2 Performance", "tab_details": "3 Details", "tab_gpu": "4 GPU",
        "sec_apps": "Apps", "sec_background": "Background processes", "sec_system": "System processes",
        "c_name": "Name", "c_cpu": "CPU", "c_mem": "Memory", "c_disk": "Disk", "c_gpu": "GPU",
        "c_gpumem": "GPU memory", "c_pid": "PID", "c_status": "Status", "c_user": "User name",
        "c_threads": "Threads", "c_cmd": "Command line",
        "st_running": "Running", "st_sleeping": "Sleeping", "st_disk-sleep": "Disk wait", "st_stopped": "Stopped",
        "st_tracing-stop": "Stopped", "st_zombie": "Zombie", "st_dead": "Dead", "st_idle": "Idle",
        "st_parked": "Sleeping", "st_waking": "Waking", "st_wake-kill": "Waking",
        "kernel": "Linux kernel",
        "search_ph": "Search name, PID or command line   Enter back to the list   Esc clears",
        "b_search": "Search", "b_end": "End task", "b_sort": "Sort", "b_reverse": "Reverse",
        "b_cores": "Each core", "b_pause": "Pause", "b_help": "Help", "b_quit": "Quit",
        "k_space": "Space",
        "cpu": "CPU", "memory": "Memory", "disk_n": "Disk {n}", "ethernet": "Ethernet", "wifi": "Wi-Fi",
        "gpu_n": "GPU {n}",
        "g_util": "% Utilization", "g_mem": "Memory usage", "g_active": "Active time %", "g_read": "Read speed",
        "g_write": "Write speed", "g_recv": "Receive", "g_sent": "Send", "g_gpumem": "Dedicated GPU memory usage",
        "g_transfer": "Transfer rate", "g_throughput": "Throughput", "lg_read": "read", "lg_write": "write",
        "lg_recv": "receive", "lg_send": "send",
        "seconds": "{n} seconds",
        "i_util": "Utilization", "i_speed": "Speed", "i_maxspeed": "Max speed", "i_procs": "Processes",
        "i_threads": "Threads", "i_uptime": "Up time", "i_cores": "Cores", "i_logical": "Logical processors",
        "i_load": "Load average", "i_inuse": "In use", "i_avail": "Available", "i_cached": "Cached",
        "i_shared": "Shared", "i_swap": "Swap", "i_total": "Total", "i_comp": "Memory composition",
        "i_free": "Free",
        "i_active": "Active time", "i_resp": "Avg. response", "i_read": "Read speed",
        "i_write": "Write speed", "i_capacity": "Capacity", "i_type": "Type", "i_model": "Model",
        "i_mounts": "Mount points",
        "i_space": "Space", "i_health": "Health (from udisks, read every minute)", "h_used": "Life used",
        "h_spare": "Spare", "h_written": "Data written", "h_read": "Data read", "h_hours": "Power-on hours",
        "h_unsafe": "Unsafe shutdowns", "h_media": "Media errors", "h_warn": "Warnings", "h_none": "none",
        "h_na": "Health not available (udisks did not answer)",
        "i_recv": "Receive", "i_sent": "Send", "i_link": "Link speed", "i_adapter": "Adapter",
        "i_ipv4": "IPv4 address", "i_ipv6": "IPv6 address", "i_total_recv": "Total received",
        "i_total_sent": "Total sent", "i_down": "Not connected",
        "i_vram": "Memory", "i_temp": "Temperature", "i_power": "Power", "i_fan": "Fan",
        "i_sm": "Core clock", "i_memclk": "Memory clock", "i_driver": "Driver version",
        "i_gpu_procs": "Processes on this GPU", "i_none": "none",
        "i_sm_busy": "SMs busy", "i_tensor": "Tensor cores", "i_bw": "Memory bandwidth", "i_pcie_tx": "PCIe send",
        "i_pcie_rx": "PCIe receive", "i_slowed": "Slowed by", "g_bw": "% Memory bandwidth",
        "sl_idle": "idle", "sl_power": "power cap", "sl_heat": "heat", "sl_hw": "HW slowdown",
        "sl_brake": "power brake", "sl_clocks": "app clocks", "sl_display": "display",
        "sl_sync": "sync boost", "sl_board": "board limit", "sl_reliability": "reliability",
        "b_zoom": "Time span", "minutes": "{n} minutes",
        "cores_hint": "l: what each core is doing", "cores_back": "l: back to overall",
        "lg_user": "programs", "lg_sys": "kernel", "lg_iow": "I/O wait", "lg_cell": "each cell: logical processor, use, clock GHz",
        "lg_pairs": "the two threads of one physical core sit together: {a} and {b}, {c} and {d}...",
        "busy_title": "The busiest logical processors, and what runs on them", "busy_none": "No core is busy right now (all under 5%)",
        "busy_kernel": "the kernel (interrupts, drivers)", "busy_note": "“—”: no program found (tiny ones are not followed; see ?)",
        "i_cpu_temp": "Temperature",
        "end_title": "End “{name}”?", "end_count": "{n} process(es):", "end_more": "... and {n} more",
        "end_explain": "End sends SIGTERM: it may clean up, then exit.\nForce sends SIGKILL: it stops at once, no clean-up.",
        "end_others": "{n} of them belong to {users}: ending those needs sudo.",
        "btn_end": "End  Enter", "btn_kill": "Force  F", "btn_cancel": "Cancel  Esc",
        "sent": "Sent {sig} to {n} process(es)", "denied": "{n} not allowed: {users}'s processes need sudo",
        "gone": "{n} already gone", "no_kernel": "Kernel threads cannot be ended", "no_init": "PID 1 cannot be ended",
        "no_self": "That is this Task Manager: press q to quit", "nothing": "Nothing on this row can be ended",
        "sample_error": "Reading failed: {err}", "help": HELP_EN,
        "cfg_error": "Could not read {path}: {err}. Project groups are off.", "cfg_none": "none set",
        "cfg_python": "it needs Python 3.11 or newer",
    },
}


class Glyphs:
    """The characters the screen is drawn with: Unicode, or plain ASCII for the Linux console."""

    def __init__(self, ascii_only: bool) -> None:
        if ascii_only:
            self.blocks = " .,:;=+*#"          # 0..8 eighths of a cell
            self.open, self.closed = "v ", "> "
            self.sort_desc, self.sort_asc = "v", "^"
            self.down, self.up = "R", "S"
            self.dash, self.dot, self.sep = "-", ".", "-"
            self.bar = ("#", "+", ".")
            self.hblocks = None              # no partial cells: one character per cell
        else:
            self.blocks = " ▁▂▃▄▅▆▇█"
            self.open, self.closed = "▾ ", "▸ "
            self.sort_desc, self.sort_asc = "▼", "▲"
            self.down, self.up = "↓", "↑"
            self.dash, self.dot, self.sep = "—", "·", "·"
            self.bar = ("█", "▓", "░")
            self.hblocks = " ▏▎▍▌▋▊▉█"      # 0..8 eighths of a cell, filled from the left


T = STRINGS[ARGS.lang]
G = Glyphs(ARGS.ascii)

HOME = Path.home()
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "taskmgr" / "groups.toml"
MB, GB = 1 << 20, 1 << 30

COLORS = {"cpu": "#4aa3df", "mem": "#b46ee6", "disk": "#6ab04c", "disk2": "#e5c07b",
          "net": "#e58e26", "net2": "#f6d365", "gpu": "#2bb5a3"}
CORE_COLORS = {"user": "#4aa3df", "sys": "#e06c75", "iow": "grey58"}   # programs, kernel, I/O wait
TRACK = "grey19"                   # the empty part of a bar
GRID = Style(color="grey30")
HEAT = ("#d7c95a", "#e8953a", "#ef5f5f")  # a little, a lot, very much


# ---------------------------------------------------------------- formatting

def fmt_bytes(n: float | None) -> str:
    if n is None:
        return G.dash
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            if unit == "B":
                return f"{n:.0f} B"
            return f"{n:.1f} {unit}" if abs(n) < 100 else f"{n:.0f} {unit}"
        n /= 1024.0
    return G.dash  # not reached


def fmt_rate(n: float | None) -> str:
    return G.dash if n is None else fmt_bytes(n) + "/s"


def fmt_mbs(n: float | None) -> str:
    """The Processes tab's Disk column: always MB/s, as in Windows."""
    if n is None:
        return G.dash
    mb = n / MB
    return "0 MB/s" if mb < 0.05 else f"{mb:.1f} MB/s"


def fmt_pct(v: float) -> str:
    return "0%" if v < 0.05 else f"{v:.1f}%"


def pct0(v: float | None) -> str:
    return G.dash if v is None else f"{v:.0f}%"


def fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f"{d}:{h:02d}:{m:02d}:{s:02d}"


def fmt_ghz(mhz: float | None) -> str:
    return G.dash if not mhz else f"{mhz / 1000:.2f} GHz"


def fmt_link(mbps: int) -> str:
    if not mbps:
        return G.dash
    return f"{mbps / 1000:g} Gbps" if mbps >= 1000 else f"{mbps} Mbps"


def fmt_secs(s: float) -> str:
    return f"{s:.0f}" if s >= 10 or float(s).is_integer() else f"{s:.1f}"


def dim_hex(color: str, f: float = 0.55) -> str:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"#{int(r * f):02x}{int(g * f):02x}{int(b * f):02x}"


def heat(v: float | None, lo: float, mid: float, hi: float) -> str:
    if v is None:
        return ""
    return HEAT[2] if v >= hi else HEAT[1] if v >= mid else HEAT[0] if v >= lo else ""


def nice_ceil(m: float) -> float:
    """A round top for a rate graph: 1, 2 or 5 of the unit the peak is shown in."""
    if m <= 0:
        return 1024.0
    unit = 1.0
    while m / unit >= 1000 and unit < 1024 ** 4:
        unit *= 1024
    x = m / unit * 1.05
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        if x <= step:
            return step * unit
    return 1024 * unit


def two_sided(left: Text | str, right: Text | str, width: int, cut: str = "left") -> Text:
    """One line: `left` at the start, `right` at the end; if they do not fit, `cut` loses the end of its text."""
    lt = left if isinstance(left, Text) else Text(left)
    rt = right if isinstance(right, Text) else Text(right)
    if lt.cell_len + rt.cell_len + 1 > width:
        if cut == "right":
            rt = rt.copy()
            rt.truncate(max(0, width - lt.cell_len - 1), overflow="ellipsis")
        else:
            lt = lt.copy()
            lt.truncate(max(0, width - rt.cell_len - 1), overflow="ellipsis")
    out = Text(no_wrap=True, overflow="ellipsis")
    out.append_text(lt)
    out.append(" " * max(1, width - lt.cell_len - rt.cell_len))
    out.append_text(rt)
    return out


class Runs:
    """Builds a line of Text one cell at a time, merging neighbours of the same style."""

    def __init__(self) -> None:
        self.text = Text(no_wrap=True)
        self.buf: list[str] = []
        self.style: Style | str | None = None

    def add(self, ch: str, style: Style | str | None) -> None:
        if style != self.style and self.buf:
            self.text.append("".join(self.buf), self.style)
            self.buf = []
        self.style = style
        self.buf.append(ch)

    def done(self) -> Text:
        if self.buf:
            self.text.append("".join(self.buf), self.style)
        return self.text


def sparkline(values: list, width: int, vmax: float | None, color: str) -> Text:
    vals = values[-width:] if width > 0 else []
    if vmax is None:
        vmax = max((v for v in vals if v is not None), default=0) or 1
    chars = [" "] * (width - len(vals))
    for v in vals:
        level = 1 if not v or vmax <= 0 else max(1, min(8, round(v / vmax * 8)))
        chars.append(G.blocks[level] if v is not None else " ")
    return Text("".join(chars), style=color, no_wrap=True)


def stacked_bar(parts: list[tuple[float, str]], width: int) -> Text:
    """A horizontal bar `width` cells long: each (percent, colour) part after the other, in eighths of a
    cell; a cell where two parts meet is drawn half one colour, half the other (fore- and background)."""
    total = width * 8
    edges, acc = [], 0.0
    for pct, color in parts:
        acc += max(0.0, pct) / 100 * total
        edges.append((min(total, int(round(acc))), color))

    def color_at(pos: int) -> str | None:
        return next((color for end, color in edges if pos < end), None)

    out = Runs()
    for c in range(width):
        cells = [color_at(c * 8 + k) for k in range(8)]
        if G.hblocks is None:  # ASCII: the colour covering most of the cell, as a letter
            main = max(set(cells), key=cells.count)
            out.add({None: " ", CORE_COLORS["user"]: "#", CORE_COLORS["sys"]: "=", CORE_COLORS["iow"]: "."}.get(main, "#"),
                    main)
            continue
        first, n = cells[0], 1
        while n < 8 and cells[n] == first:
            n += 1
        if first is None:
            out.add(" ", Style(bgcolor=TRACK))
        elif n == 8:
            out.add(G.hblocks[8], Style(color=first))
        else:
            out.add(G.hblocks[n], Style(color=first, bgcolor=cells[n] or TRACK))
    return out.done()


def resample(values: list, width: int, per: float, total: int) -> list:
    """`values`, the last of `total` readings (oldest first), as `width` columns of `per` readings each,
    the newest at the right. A column holds the mean of its readings, or, narrower than one reading, the
    reading at that moment; None before the oldest. Each column holds a fixed stretch of time, so the
    picture moves one column at a time instead of shimmering with every reading."""
    n = len(values)
    first = total - n                          # the number of values[0], counting every reading ever taken
    newest = math.floor((total - 1) / per)     # the column the newest reading falls in
    out = []
    for c in range(width):
        b = newest - (width - 1 - c)
        a0, a1 = math.ceil(b * per), math.ceil((b + 1) * per)
        if a1 <= a0:
            i = math.floor(b * per) - first
            out.append(values[i] if 0 <= i < n else None)
            continue
        got = [v for v in values[max(0, a0 - first):max(0, min(n, a1 - first))] if v is not None]
        out.append(sum(got) / len(got) if got else None)
    return out


def area_rows(values: list, width: int, rows: int, vmax: float, color: str) -> list[Text]:
    """A filled area graph, `rows` lines tall: the top cell of each column bright, the fill below it dim."""
    vals = [None] * (width - len(values)) + list(values[-width:])
    total = rows * 8
    bright, dim = Style(color=color), Style(color=dim_hex(color))
    grid_rows = {round(rows * f) for f in (0.25, 0.5, 0.75)} if rows >= 4 else set()
    levels = [None if v is None or vmax <= 0 else max(0.0, min(total, v / vmax * total)) for v in vals]
    tops = [None if lv is None or lv <= 0 else math.ceil(lv / 8) - 1 for lv in levels]
    out = []
    for r in range(rows):
        k = rows - 1 - r  # this row's cell index, counted from the bottom
        line = Runs()
        for c, lv in enumerate(levels):
            top = tops[c]
            if top is None or k > top:
                if r in grid_rows and c % 2 == 0:
                    line.add(G.dot, GRID)
                else:
                    line.add(" ", None)
            elif k < top:
                line.add(G.blocks[8], dim)
            else:
                part = lv - k * 8
                line.add(G.blocks[8 if part >= 7.5 else max(1, int(round(part)))], bright)
        out.append(line.done())
    return out


def stacked_area_rows(parts: list[tuple[list, str]], width: int, rows: int, vmax: float) -> list[Text]:
    """An area graph of several parts stacked (the first at the bottom), `rows` lines tall, in eighths of
    a cell; a cell where two parts meet is drawn half one colour, half the other (fore- and background)."""
    total = rows * 8
    padded = [[None] * (width - len(v[-width:])) + list(v[-width:]) for v, _ in parts]
    colors = [color for _, color in parts]
    tops = []                                  # per column: where each part ends, in eighths from the bottom
    for c in range(width):
        acc, ends = 0, []
        for k in range(len(parts)):
            v = padded[k][c] or 0.0
            h = 0 if vmax <= 0 or v <= 0 else max(1, int(round(v / vmax * total)))
            acc = min(total, acc + h)
            ends.append(acc)
        tops.append(ends)
    grid_rows = {round(rows * f) for f in (0.25, 0.5, 0.75)} if rows >= 4 else set()
    out = []
    for r in range(rows):
        base = (rows - 1 - r) * 8              # eighths below this row
        line = Runs()
        for c, ends in enumerate(tops):
            if ends[-1] <= base:               # nothing reaches this row
                line.add(G.dot if r in grid_rows and c % 2 == 0 else " ", GRID if r in grid_rows else None)
                continue
            cells = [next((colors[k] for k, end in enumerate(ends) if base + e < end), None) for e in range(8)]
            first, n = cells[0], 1
            while n < 8 and cells[n] == first:
                n += 1
            if n == 8:
                line.add(G.blocks[8], Style(color=first))
            else:
                line.add(G.blocks[n], Style(color=first, bgcolor=cells[n]))
        out.append(line.done())
    return out


# ---------------------------------------------------------------- what one reading holds

@dataclass
class Proc:
    pid: int
    ppid: int
    ct: float                      # create time: a pid plus its create time is one process
    name: str
    uid: int
    user: str
    status: str
    threads: int
    cmdline: list[str]
    cpu: float = 0.0               # % of the whole machine: every logical CPU busy = 100
    mem: int | None = None         # resident and private: RSS minus file-backed and shared pages
    disk: float | None = None      # bytes/s read + written; None = not ours to read
    gpu_mem: dict[int, int] = field(default_factory=dict)   # GPU index -> bytes
    gpu_util: float = 0.0
    cwd: str | None = None

    @property
    def kernel(self) -> bool:
        return self.pid == 2 or self.ppid == 2


@dataclass
class ProcGroup:
    key: str
    label: str
    section: str                   # "apps" | "background" | "system"
    members: list[Proc] = field(default_factory=list)
    cpu: float = 0.0
    mem: int = 0
    disk: float | None = None
    gpu_util: float = 0.0
    gpu_mem: dict[int, int] = field(default_factory=dict)

    @property
    def roots(self) -> list[Proc]:
        """Members whose parent is not in the group: the processes the app started from."""
        pids = {m.pid for m in self.members}
        return [m for m in self.members if m.ppid not in pids]


@dataclass
class GpuInfo:
    index: int
    name: str
    util: float | None
    mem_used: int | None
    mem_total: int | None
    temp: float | None
    power: float | None
    power_limit: float | None
    fan: float | None
    clock_sm: int | None
    clock_mem: int | None
    procs: dict[int, int]          # pid -> bytes on this card
    sm: float | None = None        # GPM, below: % of the card's SMs (its compute units) at work
    tensor: float | None = None    # % of the time the tensor cores (matrix math) ran
    mem_bw: float | None = None    # % of the memory bandwidth in use
    pcie_tx: float | None = None   # bytes/s the card sent over PCIe
    pcie_rx: float | None = None   # bytes/s it received
    slowed: int | None = None      # why the clock is below its top: NVML's clock event reasons, as bits


@dataclass
class DiskDev:
    name: str
    model: str
    size: int
    ssd: bool | None
    nvme: bool
    temp_path: str | None = None   # the drive's own sensor (NVMe "Composite"), read every second


@dataclass
class DiskInfo:
    dev: DiskDev
    read: float
    write: float
    active: float
    resp_ms: float | None
    mounts: list[str]
    temp: float | None = None
    space: list[tuple[str, int, int, float]] = field(default_factory=list)  # (mount, used, total, %)
    health: dict | None = None     # SMART data from udisks; None when it cannot be read


@dataclass
class NicInfo:
    name: str
    wifi: bool
    up: bool
    speed: int
    ipv4: list[str]
    ipv6: list[str]
    recv: float
    sent: float
    total_recv: int
    total_sent: int


@dataclass
class Snapshot:
    t: float
    cpu: float
    freq: float | None
    freq_max: float | None
    load: tuple[float, float, float]
    uptime: float
    vm: object
    swap: object
    gpus: list[GpuInfo]
    disks: list[DiskInfo]
    nics: list[NicInfo]
    procs: dict[int, Proc]
    groups: list[ProcGroup]
    nprocs: int
    nthreads: int
    per_cpu_times: list = field(default_factory=list)     # psutil cpu_times_percent, one per logical CPU
    per_cpu_freq: list[float] = field(default_factory=list)  # MHz, one per logical CPU
    cpu_temp: float | None = None
    core_top: dict[int, tuple[float, int, str]] = field(default_factory=dict)  # CPU -> (% of it, pid, thread)


# ---------------------------------------------------------------- grouping processes into apps

SHELLS = {"bash", "sh", "dash", "zsh", "fish", "ksh", "tcsh", "csh"}
# A process under one of these starts a group of its own (Windows: an app). Under anything else it
# joins its parent's group, the way a browser's renderers join the browser.
# Desktop shells and panels: what they start is an app the user opened (the Apps section).
DESKTOP_SHELLS = {"gnome-shell", "plasmashell", "krunner", "kwin_wayland", "kwin_x11", "xfce4-panel",
                  "xfdesktop", "lxqt-panel", "cinnamon", "mate-panel", "budgie-panel", "sway", "Hyprland",
                  "i3", "niri", "labwc", "wayfire", "river", "openbox"}
CONTAINERS = SHELLS | DESKTOP_SHELLS | {
    "systemd", "init", "kthreadd", "sudo", "su", "login", "sshd", "sshd-session", "tmux: server", "screen",
    "gnome-session-binary", "gdm-session-worker", "gdm-x-session", "gdm-wayland-session", "ksmserver",
    "startplasma-wayland", "startplasma-x11", "plasma_session", "xfce4-session", "lxqt-session",
    "cinnamon-session", "mate-session", "lightdm", "sddm-helper", "xinit", "dbus-daemon", "dbus-broker"}
TYPED_IN = SHELLS | {"sudo", "su", "tmux: server", "screen"}
GNOME_HELPERS = {"mutter-x11-frames", "gjs", "Xwayland", "gnome-shell-calendar-server"}
APP_ROOTS = {"gnome-terminal-server"}
ALIASES = {"gnome-terminal-server": "gnome-terminal"}
PRETTY = {"gnome-terminal-server": {"zh": "終端機", "en": "Terminal"}}  # wins over the .desktop name
INTERPRETERS = {"bash", "sh", "dash", "node", "gjs", "perl", "ruby"}
SECTIONS = ("apps", "background", "system")


def first_script(cmd: list[str], value_flags: set[str]) -> tuple[str | None, str | None]:
    """(script, module) an interpreter's command line runs; -c gives neither."""
    i = 1
    while i < len(cmd):
        a = cmd[i]
        if a == "-m" and i + 1 < len(cmd):
            return None, cmd[i + 1]
        if a in ("-c", "-e"):
            return None, None
        if a.startswith("-"):
            i += 2 if a in value_flags else 1
            continue
        return a, None
    return None, None


def script_of(p: Proc) -> str | None:
    """What an interpreter runs, as its name: 'cc_usage.py', 'http.server', 'brave-browser-stable'."""
    if not p.cmdline:
        return None
    base0 = os.path.basename(p.cmdline[0])
    if base0.startswith("python") or p.name.startswith("python"):
        script, module = first_script(p.cmdline, {"-W", "-X"})
    elif base0 in INTERPRETERS:
        script, module = first_script(p.cmdline, {"-o", "-O"})
    else:
        return None
    if module:
        return os.path.basename(module)  # gjs -m takes a path, python -m a dotted name
    return os.path.basename(script) if script else None


def opt_value(cmd: list[str], flag: str) -> str | None:
    for i, a in enumerate(cmd):
        if a == flag and i + 1 < len(cmd):
            return cmd[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def vllm_rule(p: Proc) -> tuple[str, str, str] | None:
    cmd = p.cmdline
    for i, a in enumerate(cmd):
        serve = os.path.basename(a) == "vllm" and i + 1 < len(cmd) and cmd[i + 1] == "serve"
        if serve or a == "vllm.entrypoints.openai.api_server":
            model = (cmd[i + 2] if serve and i + 2 < len(cmd) and not cmd[i + 2].startswith("-")
                     else opt_value(cmd, "--model") or "")
            port = opt_value(cmd, "--port") or "8000"
            name = opt_value(cmd, "--served-model-name") or os.path.basename(model.rstrip("/"))
            return f"vllm:{port}", f"vLLM :{port}" + (f" {G.sep} {name}" if name else ""), "apps"
    return None


@dataclass
class Project:
    """A folder listed in groups.toml: its Python programs are grouped under the project's name."""
    name: str
    dir: Path
    names: dict[str, str]          # script or module -> the name its group shows
    notes: dict[str, str]          # script or module -> a line added to the End task dialog


def tilde(path: Path) -> str:
    return str(path).replace(str(HOME), "~", 1) if str(path).startswith(str(HOME)) else str(path)


def load_projects(path: Path = CONFIG_FILE) -> tuple[list[Project], str | None]:
    """The projects in groups.toml, and what went wrong reading it (None: nothing did).

        [[project]]
        name = "Blog"                  # its programs show as "Blog · <script>" ...
        dir = "~/code/blog"
          [project.scripts."serve.py"]  # ... unless a script (or a module run with -m) has a name
          name = "Blog server"          # name_zh = "..." for --lang zh
          end_note = "Saves drafts on SIGTERM; Force does not."
    """
    if not path.exists():
        return [], None
    if tomllib is None:
        return [], T["cfg_error"].format(path=tilde(path), err=T["cfg_python"])

    def own(table: dict, key: str) -> str | None:
        v = table.get(f"{key}_{ARGS.lang}", table.get(key))
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{key} must be text")
        return v

    projects = []
    try:
        data = tomllib.loads(path.read_text())
        listed = data.get("project", [])
        if not isinstance(listed, list) or not all(isinstance(p, dict) for p in listed):
            raise ValueError("each project must be a [[project]] table")
        for i, p in enumerate(listed):
            if not isinstance(p.get("name"), str) or not isinstance(p.get("dir"), str):
                raise ValueError(f"project {i + 1} needs a name and a dir")
            if not isinstance(p.get("scripts", {}), dict):
                raise ValueError(f"the scripts of project {i + 1} must be tables")
            names, notes = {}, {}
            for script, table in p.get("scripts", {}).items():
                if not isinstance(table, dict):
                    raise ValueError(f'scripts."{script}" must be a table')
                if own(table, "name"):
                    names[script] = own(table, "name")
                if own(table, "end_note"):
                    notes[script] = own(table, "end_note")
            folder = Path(p["dir"]).expanduser()
            projects.append(Project(p["name"], (HOME / folder).resolve(), names, notes))  # relative: to ~
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        return [], T["cfg_error"].format(path=tilde(path), err=e)
    return projects, None


PROJECTS, CONFIG_ERROR = load_projects()


def project_rule(p: Proc) -> tuple[str, str, str] | None:
    """A Python program whose script is inside a project's folder, or that runs `python -m <module>`
    with that folder as its working directory. Each script and module is a group of its own."""
    if not PROJECTS or not p.cwd or not p.cmdline:
        return None
    if not (p.name.startswith("python") or p.name == "pytest"
            or os.path.basename(p.cmdline[0]).startswith("python")):
        return None
    script, module = first_script(p.cmdline, {"-W", "-X"})
    cwd = Path(p.cwd)
    where = None
    if script:
        try:
            where = (cwd / script).resolve()
        except (OSError, RuntimeError):
            return None
    for proj in PROJECTS:
        if where is not None and where.is_relative_to(proj.dir):
            part = where.name
        elif module and where is None and cwd == proj.dir:
            part = module.rsplit(".", 1)[-1]      # tools.send_event -> send_event
        else:
            continue
        label = proj.names.get(part) or f"{proj.name} {G.sep} {part}"
        return f"project:{proj.name}:{part}", label, "apps"
    return None


def end_note(group_key: str) -> str | None:
    """The groups.toml line for the End task dialog of a project's script, if it has one."""
    for proj in PROJECTS:
        prefix = f"project:{proj.name}:"
        if group_key.startswith(prefix):
            return proj.notes.get(group_key[len(prefix):])
    return None


def postgres_rule(p: Proc) -> tuple[str, str, str] | None:
    if p.name != "postgres" or p.user != "postgres":
        return None
    first = p.cmdline[0] if p.cmdline else ""
    m = re.search(r"/postgresql/(\d+)/", first) or re.match(r"postgres: (\d+)/", first)
    return "postgres", "PostgreSQL" + (f" {m.group(1)}" if m else ""), "system"


def claude_rule(p: Proc) -> tuple[str, str, str] | None:
    return ("claude", "Claude Code", "apps") if p.name == "claude" else None


RULES: tuple[Callable[[Proc], tuple[str, str, str] | None], ...] = (
    vllm_rule, project_rule, postgres_rule, claude_rule)


def exec_basename(cmd: str) -> str:
    parts = cmd.split()
    i = 0
    if parts[:1] == ["env"]:
        i = 1
    while i < len(parts) and "=" in parts[i] and not parts[i].startswith("/"):
        i += 1
    return os.path.basename(parts[i].strip('"')) if i < len(parts) else ""


def desktop_names(lang: str) -> dict[str, str]:
    """Executable name -> the app's name from its .desktop file ('brave-browser-stable' -> 'Brave Web Browser')."""
    dirs = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
            HOME / ".local/share/applications", Path("/var/lib/snapd/desktop/applications"),
            Path("/var/lib/flatpak/exports/share/applications"),
            HOME / ".local/share/flatpak/exports/share/applications"]
    keys = ("Name[zh_TW]", "Name") if lang == "zh" else ("Name",)
    names: dict[str, str] = {}
    for d in dirs:
        try:
            files = sorted(d.glob("*.desktop"))
        except OSError:
            continue
        for f in files:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            entry: dict[str, str] = {}
            main = False
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("["):
                    main = s == "[Desktop Entry]"
                elif main and "=" in s:
                    k, v = s.split("=", 1)
                    entry.setdefault(k.strip(), v.strip())
            if entry.get("NoDisplay", "").lower() == "true":
                continue
            exe = exec_basename(entry.get("Exec", ""))
            name = next((entry[k] for k in keys if entry.get(k)), None)
            if exe and name:
                names.setdefault(exe, name)
    return names


# ---------------------------------------------------------------- reading the machine

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
BOOT = psutil.boot_time()
STATES = {"R": "running", "S": "sleeping", "D": "disk-sleep", "T": "stopped", "t": "tracing-stop",
          "Z": "zombie", "X": "dead", "x": "dead", "I": "idle", "P": "parked", "W": "waking", "K": "wake-kill"}


def read_stat(pid: int) -> tuple[str, str, int, float, int, float]:
    """(comm, status, ppid, CPU seconds used, threads, create time) from /proc/<pid>/stat.

    Read directly rather than through psutil: psutil checks every pid for reuse on each call, which
    doubled the cost of one reading. The create time is psutil's formula, so the two always agree."""
    with open(f"/proc/{pid}/stat", "rb") as f:
        data = f.read()
    left, right = data.find(b"("), data.rfind(b")")
    fields = data[right + 2:].split()
    return (data[left + 1:right].decode(errors="replace"), STATES.get(fields[0].decode(), fields[0].decode()),
            int(fields[1]), (int(fields[11]) + int(fields[12])) / CLK_TCK, int(fields[17]),
            BOOT + int(fields[19]) / CLK_TCK)


def read_uid(pid: int) -> int:
    with open(f"/proc/{pid}/status", "rb") as f:
        for line in f:
            if line.startswith(b"Uid:"):
                return int(line.split()[1])  # the real uid, as psutil gives it
    return -1


def read_cmdline(pid: int) -> list[str]:
    """psutil's own rules: NUL-separated, or space-separated after a process rewrote its title."""
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        data = f.read().decode(errors="replace")
    if not data:
        return []
    sep = "\x00" if data.endswith("\x00") else " "
    if data.endswith(sep):
        data = data[:-1]
    cmd = data.split(sep)
    if sep == "\x00" and len(cmd) == 1 and " " in data:
        cmd = data.split(" ")
    return cmd


def read_mem(pid: int) -> int | None:
    """Resident memory that is the process's own: RSS minus file-backed and shared pages."""
    try:
        with open(f"/proc/{pid}/statm", "rb") as f:
            fields = f.read().split()
        return max(0, int(fields[1]) - int(fields[2])) * PAGE
    except (OSError, IndexError, ValueError):
        return None


def read_io(pid: int) -> int | None:
    """Bytes really read from and written to storage; None when the process is not ours to read."""
    try:
        with open(f"/proc/{pid}/io", "rb") as f:
            total = 0
            for line in f:
                if line.startswith((b"read_bytes:", b"write_bytes:")):
                    total += int(line.split()[1])
            return total
    except (OSError, ValueError):
        return None


def read_text(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def find_disks() -> list[DiskDev]:
    out = []
    try:
        names = sorted(os.listdir("/sys/block"))
    except OSError:
        return out
    for n in names:
        if n.startswith(("loop", "ram", "zram", "sr", "fd", "nbd")):
            continue
        base = f"/sys/block/{n}"
        size = int(read_text(f"{base}/size") or 0) * 512
        if size <= 0:
            continue
        rot = read_text(f"{base}/queue/rotational")
        out.append(DiskDev(n, read_text(f"{base}/device/model") or "", size,
                           None if rot is None else rot == "0", n.startswith("nvme"), disk_temp_sensor(n)))
    return out


def disk_temp_sensor(name: str) -> str | None:
    """The file holding a drive's own temperature: NVMe's "Composite" sensor, or a SATA drive's (drivetemp)."""
    dev = Path(f"/sys/block/{name}/device")
    inputs = sorted(dev.glob("hwmon*/temp*_input")) + sorted(dev.glob("hwmon/hwmon*/temp*_input"))
    for inp in inputs:
        if read_text(str(inp).replace("_input", "_label")) == "Composite":
            return str(inp)
    return str(inputs[0]) if inputs else None


def udisks_health(name: str) -> dict | None:
    """An NVMe drive's SMART data, from udisks (what GNOME Disks reads), without root. None if unavailable."""
    def busctl(*args: str) -> list:
        out = subprocess.run(["busctl", "--system", "--json=short", *args], capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            raise OSError(out.stderr.strip())
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]

    nvme = "org.freedesktop.UDisks2.NVMe.Controller"
    try:
        drive = busctl("get-property", "org.freedesktop.UDisks2", f"/org/freedesktop/UDisks2/block_devices/{name}",
                       "org.freedesktop.UDisks2.Block", "Drive")[0]["data"]
        attrs = busctl("call", "org.freedesktop.UDisks2", drive, nvme, "SmartGetAttributes", "a{sv}", "0")[0]["data"][0]
        hours, warnings = busctl("get-property", "org.freedesktop.UDisks2", drive, nvme,
                                 "SmartPowerOnHours", "SmartCriticalWarning")
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return None
    health = {k: v["data"] for k, v in attrs.items()}
    health["power_on_hours"], health["warnings"] = hours["data"], warnings["data"]
    return health


def parent_disk(name: str, depth: int = 0) -> str | None:
    """'nvme0n1p2' -> 'nvme0n1'; a device-mapper volume -> the disk under it."""
    real = os.path.realpath(f"/sys/class/block/{name}")
    if not os.path.exists(real) or depth > 4:
        return None
    if os.path.exists(os.path.join(real, "partition")):
        return os.path.basename(os.path.dirname(real))
    slaves = os.path.join(real, "slaves")
    if os.path.isdir(slaves):
        for s in sorted(os.listdir(slaves)):
            found = parent_disk(s, depth + 1)
            if found:
                return found
    return name


def cpu_layout(n: int, amd: bool, base: str = "/sys/devices/system/cpu") -> list[tuple[str, list[int]]]:
    """Logical CPUs grouped the way the chip is built: by the L3 cache they share (on AMD chips, one group
    per CCD), with the two threads of each physical core next to each other (0, 16, 1, 17...)."""
    groups: dict[str, list[int]] = {}
    for i in range(n):
        groups.setdefault(read_text(f"{base}/cpu{i}/cache/index3/shared_cpu_list") or "all", []).append(i)
    ordered = sorted(groups.values(), key=lambda cpus: cpus[0])
    dies = [read_text(f"{base}/cpu{cpus[0]}/topology/die_id") for cpus in ordered]
    use_die = amd and None not in dies and len(set(dies)) == len(dies)
    out = []
    for k, cpus in enumerate(ordered):
        core = {i: int(read_text(f"{base}/cpu{i}/topology/core_id") or i) for i in cpus}
        out.append((f"CCD {dies[k]}" if use_die else f"L3 {k}", sorted(cpus, key=lambda i: (core[i], i))))
    return out


def find_cpu_temp(root: str = "/sys/class/hwmon") -> str | None:
    """The file holding the CPU package temperature (AMD Tctl / Intel Package id 0), read each second."""
    for d in sorted(Path(root).glob("hwmon*")):
        if read_text(f"{d}/name") not in ("k10temp", "zenpower", "coretemp", "cpu_thermal"):
            continue
        inputs = sorted(d.glob("temp*_input"))
        for inp in inputs:
            if read_text(str(inp).replace("_input", "_label")) in ("Tctl", "Tdie", "Package id 0"):
                return str(inp)
        if inputs:
            return str(inputs[0])
    return None


def find_nics() -> list[str]:
    stats = psutil.net_if_stats()
    skip = ("lo", "veth", "docker", "br-", "virbr", "vnet", "vmnet")
    up = sorted(n for n, s in stats.items() if s.isup and not n.startswith(skip))
    return up or sorted(n for n in stats if n != "lo")


def _s(v: object) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


class Gpm:
    """One card's GPM readings (NVML's "GPU performance monitoring", on Hopper and newer data-center and
    workstation cards): how busy the card really is. GPM compares two samples, so each reading takes one
    and compares it with the one before."""

    METRICS = ("SM_UTIL", "ANY_TENSOR_UTIL", "DRAM_BW_UTIL", "PCIE_TX_PER_SEC", "PCIE_RX_PER_SEC")

    def __init__(self, handle) -> None:
        self.handle = handle
        self.samples = [nv.nvmlGpmSampleAlloc(), nv.nvmlGpmSampleAlloc()]   # the one before, the new one
        self.taken = False                                                  # the one before holds a sample
        self.req = nv.c_nvmlGpmMetricsGet_t()
        self.req.version = nv.NVML_GPM_METRICS_GET_VERSION
        self.req.numMetrics = len(self.METRICS)
        for k, name in enumerate(self.METRICS):
            self.req.metrics[k].metricId = getattr(nv, f"NVML_GPM_METRIC_{name}")

    def read(self) -> list[float | None]:
        """METRICS since the last reading, in that order (PCIe in MiB/s); None: not known (yet)."""
        out: list[float | None] = [None] * len(self.METRICS)
        before, new = self.samples
        try:
            nv.nvmlGpmSampleGet(self.handle, new)
        except Exception:
            self.taken = False
            return out
        if self.taken:
            try:
                self.req.sample1, self.req.sample2 = before, new
                nv.nvmlGpmMetricsGet(self.req)
                out = [m.value if m.nvmlReturn == 0 else None for m in self.req.metrics[:len(self.METRICS)]]
            except Exception:
                pass
        self.samples, self.taken = [new, before], True
        return out

    def free(self) -> None:
        for s in self.samples:
            nv.nvmlGpmSampleFree(s)


def gpm_for(handle) -> Gpm | None:
    try:
        return Gpm(handle) if nv.nvmlGpmQueryDeviceSupport(handle).isSupportedDevice else None
    except Exception:              # an older card, driver or pynvml
        return None


class Gpus:
    """NVIDIA cards through NVML, numbered the way nvidia-smi numbers them."""

    def __init__(self) -> None:
        self.handles: list = []
        self.names: list[str] = []
        self.driver = ""
        self.lock = threading.Lock()   # a reading (in the worker thread) and close() never overlap
        self.closed = False
        self.gpm: list[Gpm | None] = []
        if nv is None:
            return
        try:
            nv.nvmlInit()
            self.handles = [nv.nvmlDeviceGetHandleByIndex(i) for i in range(nv.nvmlDeviceGetCount())]
            self.names = [_s(nv.nvmlDeviceGetName(h)) for h in self.handles]
            self.driver = _s(nv.nvmlSystemGetDriverVersion())
        except Exception:
            self.handles, self.names = [], []
        self.last_ts = [0] * len(self.handles)
        self.mem_v2 = hasattr(nv, "nvmlMemory_v2")
        self.gpm = [gpm_for(h) for h in self.handles]
        self.slowed_fn = (getattr(nv, "nvmlDeviceGetCurrentClocksEventReasons", None)       # its newer name
                          or getattr(nv, "nvmlDeviceGetCurrentClocksThrottleReasons", None))

    @property
    def has_gpm(self) -> bool:
        return any(self.gpm)

    @staticmethod
    def _call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            return None

    def close(self) -> None:
        """Shuts NVML down once no reading is using it: a reading still running when NVML shuts down
        hangs or crashes inside NVIDIA's library. Readings after this find no cards. A reading takes
        milliseconds; if one hangs instead, NVML is left to the end of the process."""
        self.closed = True
        if not self.lock.acquire(timeout=2):
            return
        try:
            for g in self.gpm:
                if g:
                    self._call(g.free)
            if self.handles:
                self._call(nv.nvmlShutdown)
        finally:
            self.lock.release()

    def sample(self) -> tuple[list[GpuInfo], dict[int, dict[int, int]], dict[int, dict[int, float]]]:
        with self.lock:
            return ([], {}, {}) if self.closed else self._sample()

    def _sample(self) -> tuple[list[GpuInfo], dict[int, dict[int, int]], dict[int, dict[int, float]]]:
        infos: list[GpuInfo] = []
        mem_by_pid: dict[int, dict[int, int]] = {}
        util_by_pid: dict[int, dict[int, float]] = {}
        c = self._call
        for i, h in enumerate(self.handles):
            # v2 leaves out what the driver reserves, so "used" matches nvidia-smi
            mem = c(nv.nvmlDeviceGetMemoryInfo, h, version=nv.nvmlMemory_v2) if self.mem_v2 else None
            mem = mem or c(nv.nvmlDeviceGetMemoryInfo, h)
            util = c(nv.nvmlDeviceGetUtilizationRates, h)
            power = c(nv.nvmlDeviceGetPowerUsage, h)
            limit = c(nv.nvmlDeviceGetEnforcedPowerLimit, h)
            procs: dict[int, int] = {}
            for lst in (c(nv.nvmlDeviceGetComputeRunningProcesses, h) or [],
                        c(nv.nvmlDeviceGetGraphicsRunningProcesses, h) or []):
                for pr in lst:
                    used = pr.usedGpuMemory or 0
                    procs[pr.pid] = max(procs.get(pr.pid, 0), used)  # a process can be in both lists
            for pid, used in procs.items():
                mem_by_pid.setdefault(pid, {})[i] = used
            samples = c(nv.nvmlDeviceGetProcessUtilization, h, self.last_ts[i])
            if samples:
                acc: dict[int, list[float]] = {}
                for smp in samples:
                    acc.setdefault(smp.pid, []).append(smp.smUtil)
                    self.last_ts[i] = max(self.last_ts[i], smp.timeStamp)
                for pid, us in acc.items():
                    util_by_pid.setdefault(pid, {})[i] = sum(us) / len(us)
            sm, tensor, bw, tx, rx = self.gpm[i].read() if self.gpm[i] else (None,) * 5
            infos.append(GpuInfo(
                index=i, name=self.names[i] if i < len(self.names) else f"GPU {i}",
                util=float(util.gpu) if util else None,
                mem_used=mem.used if mem else None, mem_total=mem.total if mem else None,
                temp=c(nv.nvmlDeviceGetTemperature, h, nv.NVML_TEMPERATURE_GPU),
                power=power / 1000 if power is not None else None,
                power_limit=limit / 1000 if limit is not None else None,
                fan=c(nv.nvmlDeviceGetFanSpeed, h),
                clock_sm=c(nv.nvmlDeviceGetClockInfo, h, nv.NVML_CLOCK_SM),
                clock_mem=c(nv.nvmlDeviceGetClockInfo, h, nv.NVML_CLOCK_MEM),
                procs=procs, sm=sm, tensor=tensor, mem_bw=bw,
                pcie_tx=None if tx is None else tx * MB, pcie_rx=None if rx is None else rx * MB,
                slowed=c(self.slowed_fn, h) if self.slowed_fn else None))
        return infos, mem_by_pid, util_by_pid


class Sampler:
    """Takes one reading of the whole machine. Runs off the UI thread, one reading at a time."""

    def __init__(self, lang: str) -> None:
        self.ncpu = psutil.cpu_count() or 1
        self.ncores = psutil.cpu_count(logical=False) or self.ncpu
        self.cpu_model = cpu_model()
        self.me = os.getuid()
        self.desk = desktop_names(lang)
        self.users: dict[int, str] = {}
        self.prev_t: float | None = None
        self.prev_proc: dict[tuple[int, float], tuple[float | None, int | None]] = {}
        self.cwd_cache: dict[tuple[int, float], str | None] = {}
        self.rule_cache: dict[tuple[int, float], tuple[str, str, str] | None] = {}
        self.disks = find_disks()
        self.prev_disk: dict[str, object] = {}
        self.health: dict[str, tuple[float, dict | None]] = {}
        self.mounts: dict[str, list[str]] = {}
        self.mounts_at = 0.0
        self.nics = find_nics()
        self.prev_nic: dict[str, object] = {}
        self.gpus = Gpus()
        self.boot = psutil.boot_time()
        self.cpu_groups = cpu_layout(self.ncpu, "AMD" in self.cpu_model)
        self.temp_path = find_cpu_temp()
        self.freq_paths = [f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq" for i in range(self.ncpu)]
        self.prev_threads: dict[tuple[int, int], float] = {}
        psutil.cpu_percent(None)
        psutil.cpu_times_percent(None, percpu=True)

    def user(self, uid: int) -> str:
        if uid not in self.users:
            try:
                self.users[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self.users[uid] = str(uid)
        return self.users[uid]

    def sample(self) -> Snapshot:
        now = time.monotonic()
        dt = now - self.prev_t if self.prev_t else None
        self.prev_t = now
        cpu = psutil.cpu_percent(None)
        try:
            freq = psutil.cpu_freq()
        except Exception:
            freq = None
        per_times = psutil.cpu_times_percent(None, percpu=True)
        per_freq = [float(read_text(path) or 0) / 1000 for path in self.freq_paths]
        temp = read_text(self.temp_path) if self.temp_path else None
        gpus, gmem, gutil = self.gpus.sample()
        procs = self.sample_procs(dt, gmem, gutil)
        user_procs = [p for p in procs.values() if not p.kernel]
        return Snapshot(
            per_cpu_times=per_times, per_cpu_freq=per_freq, cpu_temp=int(temp) / 1000 if temp else None,
            core_top=self.thread_usage(procs, dt),
            t=time.time(), cpu=cpu,
            freq=freq.current if freq else None, freq_max=freq.max if freq and freq.max else None,
            load=os.getloadavg(), uptime=time.time() - self.boot,
            vm=psutil.virtual_memory(), swap=psutil.swap_memory(),
            gpus=gpus, disks=self.sample_disks(dt), nics=self.sample_nics(dt),
            procs=procs, groups=self.group(procs),
            nprocs=len(user_procs), nthreads=sum(p.threads for p in user_procs))

    def sample_procs(self, dt: float | None, gmem: dict, gutil: dict) -> dict[int, Proc]:
        procs: dict[int, Proc] = {}
        prev, cwds = {}, {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                comm, status, ppid, cpu_s, threads, ct = read_stat(pid)
                kernel = pid == 2 or ppid == 2
                # kernel threads (on a machine with many cores, most of the processes) have nothing more to read
                uid = 0 if kernel else read_uid(pid)
                cmd = [] if kernel else read_cmdline(pid)
            except (OSError, ValueError, IndexError):
                continue  # gone between the listing and the reading
            key = (pid, ct)
            io_b = None if kernel else read_io(pid)
            last = self.prev_proc.get(key)
            cpu, disk = 0.0, (None if io_b is None else 0.0)
            if last and dt:
                cpu = max(0.0, (cpu_s - last[0]) / dt / self.ncpu * 100)
                if io_b is not None and last[1] is not None:
                    disk = max(0.0, (io_b - last[1]) / dt)
            prev[key] = (cpu_s, io_b)
            name = comm
            if len(comm) >= 15 and cmd:  # /proc cuts names at 15 characters; psutil does the same
                base = os.path.basename(cmd[0])
                if base.startswith(comm):
                    name = base
            cwd = None
            if name.startswith("python") or name == "pytest" or (cmd and os.path.basename(cmd[0]).startswith("python")):
                if key in self.cwd_cache:
                    cwd = self.cwd_cache[key]
                else:
                    try:
                        cwd = os.readlink(f"/proc/{pid}/cwd")
                    except OSError:
                        cwd = None
                cwds[key] = cwd
            procs[pid] = Proc(
                pid=pid, ppid=ppid, ct=ct, name=name, uid=uid, user=self.user(uid), status=status,
                threads=threads, cmdline=cmd, cpu=cpu, mem=None if kernel else read_mem(pid), disk=disk,
                gpu_mem=gmem.get(pid, {}), gpu_util=min(100.0, sum(gutil.get(pid, {}).values())), cwd=cwd)
        self.prev_proc, self.cwd_cache = prev, cwds
        return procs

    def thread_usage(self, procs: dict[int, Proc], dt: float | None) -> dict[int, tuple[float, int, str]]:
        """Logical CPU -> (% of that CPU, pid, thread name) of the busiest thread that last ran on it.

        Only the busiest processes are opened thread by thread (the machine has thousands of threads,
        and reading them all every second would cost more than everything else together). A thread
        that moved between CPUs during the second counts on the one it was on last."""
        # programs over 3% of one CPU (p.cpu is a share of the whole machine)
        busy = sorted((p for p in procs.values() if p.cpu * self.ncpu >= 3), key=lambda p: p.cpu, reverse=True)[:12]
        prev: dict[tuple[int, int], float] = {}
        top: dict[int, tuple[float, int, str]] = {}
        for p in busy:
            try:
                tids = os.listdir(f"/proc/{p.pid}/task")
            except OSError:
                continue
            for tid in tids:
                try:
                    with open(f"/proc/{p.pid}/task/{tid}/stat", "rb") as f:
                        data = f.read()
                    left, right = data.find(b"("), data.rfind(b")")
                    fields = data[right + 2:].split()
                    used = (int(fields[11]) + int(fields[12])) / CLK_TCK
                    where, key = int(fields[36]), (int(tid), int(fields[19]))
                except (OSError, ValueError, IndexError):
                    continue
                prev[key] = used
                last = self.prev_threads.get(key)
                if last is None or not dt:
                    continue
                pct = max(0.0, (used - last) / dt * 100)
                if pct > top.get(where, (0.0,))[0]:
                    top[where] = (pct, p.pid, data[left + 1:right].decode(errors="replace"))
        self.prev_threads = prev
        return top

    def rule(self, p: Proc) -> tuple[str, str, str] | None:
        key = (p.pid, p.ct)
        if key not in self.rule_cache:
            self.rule_cache[key] = next((r for r in (rule(p) for rule in RULES) if r), None)
        return self.rule_cache[key]

    def app_label(self, p: Proc) -> tuple[str, bool]:
        """(the name a group shows, whether it came from a .desktop file)."""
        if p.name in PRETTY:
            return PRETTY[p.name][ARGS.lang], True
        script = script_of(p)
        candidates = ([script] if script else []) + [ALIASES.get(p.name, p.name)]
        if p.cmdline:
            candidates.append(os.path.basename(p.cmdline[0]))
        for c in candidates:
            if c in self.desk:
                return self.desk[c], True
        return ("GNOME Shell" if p.name == "gnome-shell" else script or p.name), False

    def section_of(self, p: Proc, parent: Proc | None, from_desktop: bool) -> str:
        if p.uid != self.me:
            return "system"
        pname = parent.name if parent else ""
        if pname in TYPED_IN:
            return "apps"                      # typed in a terminal
        if pname in DESKTOP_SHELLS and p.name not in GNOME_HELPERS:
            return "apps"                      # opened from the desktop
        if p.name in APP_ROOTS or from_desktop:
            return "apps"
        return "background"

    def group(self, procs: dict[int, Proc]) -> list[ProcGroup]:
        memo: dict[int, tuple[str, str, str]] = {}

        def assign(pid: int, depth: int = 0) -> tuple[str, str, str]:
            if pid in memo:
                return memo[pid]
            p = procs[pid]
            if p.kernel:
                res = ("kernel", T["kernel"], "system")
            else:
                res = self.rule(p)
                if res is None:
                    parent = procs.get(p.ppid)
                    if parent is None or parent.pid <= 1 or parent.name in CONTAINERS or depth > 64:
                        label, from_desktop = self.app_label(p)
                        section = self.section_of(p, parent, from_desktop)
                        res = (f"app:{section}:{label}", label, section)
                    else:
                        res = assign(parent.pid, depth + 1)
            memo[pid] = res
            return res

        groups: dict[str, ProcGroup] = {}
        for p in procs.values():
            key, label, section = assign(p.pid)
            g = groups.get(key)
            if g is None:
                g = groups[key] = ProcGroup(key, label, section)
            g.members.append(p)
            g.cpu += p.cpu
            g.mem += p.mem or 0
            if p.disk is not None:
                g.disk = (g.disk or 0.0) + p.disk
            for idx, b in p.gpu_mem.items():
                g.gpu_mem[idx] = g.gpu_mem.get(idx, 0) + b
            g.gpu_util = min(100.0, g.gpu_util + p.gpu_util)
        alive = {(p.pid, p.ct) for p in procs.values()}
        self.rule_cache = {k: v for k, v in self.rule_cache.items() if k in alive}
        return list(groups.values())

    def sample_disks(self, dt: float | None) -> list[DiskInfo]:
        if time.monotonic() - self.mounts_at > 30:
            mounts: dict[str, list[str]] = {}
            for part in psutil.disk_partitions(all=False):
                d = parent_disk(os.path.basename(part.device))
                if d:
                    mounts.setdefault(d, []).append(part.mountpoint)
            self.mounts, self.mounts_at = mounts, time.monotonic()
        counters = psutil.disk_io_counters(perdisk=True, nowrap=True) or {}
        out = []
        for d in self.disks:
            c, p = counters.get(d.name), self.prev_disk.get(d.name)
            read = write = active = 0.0
            resp = None
            if c and p and dt:
                read = max(0, c.read_bytes - p.read_bytes) / dt
                write = max(0, c.write_bytes - p.write_bytes) / dt
                active = min(100.0, max(0.0, (c.busy_time - p.busy_time) / (dt * 1000) * 100))
                n_io = (c.read_count + c.write_count) - (p.read_count + p.write_count)
                t_io = (c.read_time + c.write_time) - (p.read_time + p.write_time)
                resp = t_io / n_io if n_io > 0 else 0.0
            if c:
                self.prev_disk[d.name] = c
            raw = read_text(d.temp_path) if d.temp_path else None
            space = []
            for mount in sorted(self.mounts.get(d.name, [])):
                try:
                    u = psutil.disk_usage(mount)
                except OSError:
                    continue
                space.append((mount, u.used, u.total, u.percent))
            out.append(DiskInfo(d, read, write, active, resp, self.mounts.get(d.name, []),
                                temp=int(raw) / 1000 if raw and raw.lstrip("-").isdigit() else None,
                                space=space, health=self.disk_health(d)))
        return out

    def disk_health(self, d: DiskDev) -> dict | None:
        """udisks' SMART data, asked for once a minute (udisks itself refreshes it every few minutes);
        after a failure, again in five."""
        if not d.nvme:
            return None
        at, health = self.health.get(d.name, (float("-inf"), None))
        if time.monotonic() - at > (60 if health else 300):
            health = udisks_health(d.name)
            self.health[d.name] = (time.monotonic(), health)
        return health

    def sample_nics(self, dt: float | None) -> list[NicInfo]:
        counters = psutil.net_io_counters(pernic=True, nowrap=True)
        stats, addrs = psutil.net_if_stats(), psutil.net_if_addrs()
        out = []
        for n in self.nics:
            c, p = counters.get(n), self.prev_nic.get(n)
            recv = sent = 0.0
            if c and p and dt:
                recv = max(0, c.bytes_recv - p.bytes_recv) / dt
                sent = max(0, c.bytes_sent - p.bytes_sent) / dt
            if c:
                self.prev_nic[n] = c
            st = stats.get(n)
            out.append(NicInfo(
                name=n, wifi=os.path.isdir(f"/sys/class/net/{n}/wireless"), up=bool(st and st.isup),
                speed=st.speed if st else 0,
                ipv4=[a.address for a in addrs.get(n, []) if a.family == socket.AF_INET],
                ipv6=[a.address.split("%")[0] for a in addrs.get(n, []) if a.family == socket.AF_INET6],
                recv=recv, sent=sent, total_recv=c.bytes_recv if c else 0, total_sent=c.bytes_sent if c else 0))
        return out


class History:
    """The last readings of every graphed number, newest last."""

    def __init__(self, size: int = 900) -> None:
        self.size = size
        self.data: dict[str, deque] = {}
        self.count = 0                 # readings pushed so far

    def add(self, key: str, value: float | None) -> None:
        d = self.data.get(key)
        if d is None:
            d = self.data[key] = deque(maxlen=self.size)
        d.append(value)

    def get(self, key: str) -> list:
        return list(self.data.get(key, ()))

    def push(self, s: Snapshot) -> None:
        self.count += 1
        self.add("cpu", s.cpu)
        self.add("mem", s.vm.percent)
        for g in s.gpus:
            self.add(f"gpu{g.index}.util", g.util or 0)
            self.add(f"gpu{g.index}.mem", g.mem_used or 0)
            self.add(f"gpu{g.index}.bw", g.mem_bw)          # None: no GPM, or its first reading
        for d in s.disks:
            self.add(f"disk.{d.dev.name}.active", d.active)
            self.add(f"disk.{d.dev.name}.read", d.read)
            self.add(f"disk.{d.dev.name}.write", d.write)
        for n in s.nics:
            self.add(f"nic.{n.name}.recv", n.recv)
            self.add(f"nic.{n.name}.sent", n.sent)


# ---------------------------------------------------------------- widgets

@dataclass
class Series:
    label: str
    values: list
    vmax: float | None             # None: scale to the peak on screen (rates)
    top: str | None                # the label at the top right; None: the auto scale's own
    color: str
    parts: list[tuple[str, list, str]] | None = None   # stacked: (name, values, colour), bottom first


class Graph(Widget):
    """One or more filled area graphs stacked, sharing a time axis along the bottom."""

    DEFAULT_CSS = "Graph { height: 1fr; min-height: 4; }"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.series: list[Series] = []
        self.step = 1.0
        self.span: float | None = None   # seconds across; None: one reading to a column
        self.total = 0                   # readings taken so far (the history keeps the last of them)

    def set_series(self, series: list[Series], step: float, span: float | None = None, total: int = 0) -> None:
        self.series, self.step, self.span, self.total = series, step, span, total
        self.refresh()

    def render(self) -> Text:
        w, h = self.size.width, self.size.height
        if not self.series or w < 8 or h < 3:
            return Text("")
        if self.span and self.total:
            per_col = self.span / self.step / w
            fit = lambda values: resample(values, w, per_col, self.total)  # noqa: E731
        else:
            fit = lambda values: values[-w:]  # noqa: E731
        lines: list[Text] = []
        base, extra = divmod(h - 1, len(self.series))   # rows per graph; the first ones get what is left over
        for n, s in enumerate(self.series):
            per = base + (n < extra)
            if s.parts:                         # stacked parts, scaled to the peak of their sum
                parts = [(name, fit(values), color) for name, values, color in s.parts]
                sums = [sum(v or 0 for v in col) for col in zip(*(values for _, values, _ in parts))]
                vmax = nice_ceil(max(sums, default=0))
                label = Text(s.label, style="dim")
                for name, _, color in parts:
                    label.append("   ")
                    label.append(G.bar[0], color)
                    label.append(" " + name, "dim")
                lines.append(two_sided(label, Text(fmt_rate(vmax), style="dim"), w))
                if per - 1 >= 1:
                    lines.extend(stacked_area_rows([(values, color) for _, values, color in parts], w, per - 1, vmax))
                continue
            vals = fit(s.values)
            vmax, top = s.vmax, s.top
            if vmax is None:
                vmax = nice_ceil(max((v for v in vals if v is not None), default=0))
                top = fmt_rate(vmax)
            lines.append(two_sided(Text(s.label, style="dim"), Text(top or "", style="dim"), w))
            if per - 1 >= 1:
                lines.extend(area_rows(vals, w, per - 1, vmax, s.color))
        while len(lines) < h - 1:
            lines.append(Text(""))
        if self.span and self.total:
            span = T["minutes"].format(n=fmt_secs(self.span / 60))
        else:
            span = T["seconds"].format(n=fmt_secs(w * self.step))
        lines.append(two_sided(Text(span, style="dim"), Text("0", style="dim"), w))
        return Text("\n", no_wrap=True, overflow="ellipsis").join(lines)


class CoreGrid(Widget):
    """What each logical processor is doing: a bar split into programs / kernel / I/O wait, the use in %,
    its clock. One column per CCD when they fit side by side; otherwise as many columns as fit."""

    DEFAULT_CSS = "CoreGrid { height: auto; }"
    GAP = 3

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.groups: list[tuple[str, list[int]]] = []
        self.times: list = []
        self.freq: list[float] = []

    def set_data(self, groups: list[tuple[str, list[int]]], times: list, freq: list[float]) -> None:
        self.groups, self.times, self.freq = groups, times, freq
        self.refresh(layout=True)

    @staticmethod
    def split(t) -> tuple[float, float, float]:
        """(programs, kernel, I/O wait) in % of one CPU, from psutil's cpu_times_percent."""
        return t.user + t.nice, t.system + t.irq + t.softirq + t.steal, t.iowait

    def cell(self, i: int, bar_w: int, cell_w: int) -> Text:
        user, kern, iow = self.split(self.times[i])
        busy = min(100.0, user + kern)
        ghz = self.freq[i] / 1000 if i < len(self.freq) else 0.0
        t = Text(no_wrap=True)
        t.append(f"{i:>2} ", "dim")
        t.append_text(stacked_bar([(user, CORE_COLORS["user"]), (kern, CORE_COLORS["sys"]), (iow, CORE_COLORS["iow"])],
                                  bar_w))
        t.append(f" {busy:>3.0f}%", heat(busy, 10, 40, 80) or "")
        t.append(f" {ghz:>3.1f}" if ghz else f" {G.dash:>3}", "dim" if ghz < 2 else "")
        t.append(" " * max(0, cell_w - t.cell_len))
        return t

    def render(self) -> Text:
        w = self.size.width
        if not self.times or w < 16:
            return Text("")
        legend = Text(no_wrap=True, overflow="ellipsis")
        for key, label in (("user", "lg_user"), ("sys", "lg_sys"), ("iow", "lg_iow")):
            legend.append(G.bar[0], CORE_COLORS[key])
            legend.append(f" {T[label]}   ")
        legend.append(T["lg_cell"], "dim")
        lines = [legend]
        first = self.groups[0][1] if self.groups else []
        if len(first) >= 4 and first[0] + len(self.times) // 2 == first[1]:
            lines.append(Text(T["lg_pairs"].format(a=first[0], b=first[1], c=first[2], d=first[3]),
                              style="dim", no_wrap=True, overflow="ellipsis"))
        lines.append(Text(""))
        cell_min = 15                     # "NN " + a 3-cell bar + " 100%" + " 5.6"
        if len(self.groups) > 1 and len(self.groups) * (cell_min + self.GAP) - self.GAP <= w:
            cols = self.groups
        else:                             # not enough room for one column per CCD
            flat = [i for _, ids in self.groups for i in ids] or list(range(len(self.times)))
            ncols = max(1, (w + self.GAP) // (cell_min + self.GAP))
            per = math.ceil(len(flat) / ncols)
            cols = [("", flat[k * per:(k + 1) * per]) for k in range(ncols)]
        cell_w = (w - self.GAP * (len(cols) - 1)) // len(cols)
        bar_w = max(3, min(20, cell_w - 12))
        if any(label for label, _ in cols):
            head = Text(no_wrap=True)
            for k, (label, ids) in enumerate(cols):
                avg = sum(min(100.0, sum(self.split(self.times[i])[:2])) for i in ids) / max(1, len(ids))
                part = Text.assemble((label, "bold"), (f"  {avg:.0f}%", "dim"))
                head.append(" " * (self.GAP if k else 0))
                head.append_text(part)
                head.append(" " * max(0, cell_w - part.cell_len))
            lines.append(head)
        for r in range(max(len(ids) for _, ids in cols)):
            line = Text(no_wrap=True)
            for k, (_, ids) in enumerate(cols):
                if k:
                    line.append(" " * self.GAP)
                line.append_text(self.cell(ids[r], bar_w, cell_w) if r < len(ids) else Text(" " * cell_w))
            lines.append(line)
        return Text("\n", no_wrap=True, overflow="ellipsis").join(lines)


class PerfItem(ListItem):
    """One entry in the Performance tab's left-hand list: name, value, a small graph."""

    def __init__(self, key: str) -> None:
        super().__init__(Static(classes="pi-body"), id="pi-" + re.sub(r"[^A-Za-z0-9_-]", "_", key))
        self.key = key


class GpuPanel(Vertical):
    """One card on the GPU tab: its name, graphs of its use and memory, its numbers, the processes on it."""

    def __init__(self, index: int) -> None:
        super().__init__(id=f"gpu-{index}", classes="gpu-panel")
        self.index = index

    def compose(self) -> ComposeResult:
        yield Static(classes="gp-title")
        yield Graph(classes="gp-graph")
        yield Static(classes="gp-info")

    def on_resize(self) -> None:
        """Its text is cut to its size, so a new size needs a redraw (the tab shown, the window resized,
        the cards placed anew). Every panel is as big as the first, so the first asks for all."""
        if self.index == 0:
            self.app.call_after_refresh(self.app.render_now)


class GpuBox(VerticalScroll):
    """The GPU tab: the cards side by side, as many across as fit, filling the screen. More cards than
    fit across wrap into rows. A panel too short to show all it has keeps its height and the tab scrolls."""

    MIN_W = 36                   # one card's panel: narrower shows too little

    def __init__(self, count: int, min_height: Callable[[int], int], **kw) -> None:
        super().__init__(Grid(*[GpuPanel(i) for i in range(count)], id="gpu-grid"), **kw)
        self.min_height = min_height                   # a panel's content width -> the least height it needs
        self.placed: tuple[int, int, int] | None = None

    def on_resize(self) -> None:
        self.place_cards()

    def place_cards(self) -> None:
        size = self.scrollable_content_region.size     # without the scrollbar, when there is one
        panels = list(self.query(GpuPanel))
        if not panels or size.width < 1 or size.height < 1:
            return                                     # the tab is hidden
        across = max(1, min(len(panels), size.width // self.MIN_W))
        rows = math.ceil(len(panels) / across)
        cols = math.ceil(len(panels) / rows)           # rows as even as can be: four cards 2 x 2, not 3 + 1
        col_w = size.width // cols                     # every column as wide, so the graphs span the same time
        least = self.min_height(col_w - panels[0].styles.gutter.width)
        row_h = max(least, (size.height - (rows - 1)) // rows)   # a blank line between rows
        if (cols, col_w, row_h) == self.placed:
            return
        self.placed = (cols, col_w, row_h)
        grid = self.query_one("#gpu-grid", Grid)
        grid.styles.grid_size_columns = cols
        grid.styles.grid_columns = str(col_w)
        grid.styles.grid_rows = str(row_h)
        grid.styles.height = rows * row_h + rows - 1
        for i, p in enumerate(panels):
            p.set_class(i % cols > 0, "gp-right")      # a line on its left, like the Performance list's


class EndTaskScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape,q", "cancel", show=False), Binding("f", "force", show=False)]

    def __init__(self, title: str, body: Text) -> None:
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self._title, style="bold"), id="dlg-title")
            yield Static(self._body, id="dlg-body")
            with Horizontal(id="dlg-buttons"):
                yield Button(T["btn_end"], id="term", variant="error")
                yield Button(T["btn_kill"], id="kill", variant="warning")
                yield Button(T["btn_cancel"], id="cancel")

    def on_mount(self) -> None:
        self.query_one("#term", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_force(self) -> None:
        self.dismiss("kill")


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark,enter", "close", show=False)]

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(Text(self._text))

    def action_close(self) -> None:
        self.dismiss(None)


@dataclass
class Col:
    key: str
    label: Text
    width: int


SPANS = (None, 300, 900)   # z steps through these: one reading to a column, 5 minutes, 15 minutes
PROC_COLS = ("cpu", "mem", "disk", "gpu", "gpumem")
PROC_W = {"cpu": 8, "mem": 10, "disk": 10, "gpu": 6, "gpumem": 15}
DET_COLS = ("pid", "status", "user", "cpu", "mem", "threads", "gpumem", "cmd")
DET_W = {"pid": 7, "status": 8, "user": 9, "cpu": 6, "mem": 10, "threads": 6, "gpumem": 15}
NUMERIC = {"pid", "cpu", "mem", "disk", "gpu", "gpumem", "threads"}
BIGGEST_FIRST = {"cpu", "mem", "disk", "gpu", "gpumem", "threads"}
SORT_ORDER = {"proc": ["cpu", "mem", "disk", "gpu", "gpumem", "name"],
              "details": ["cpu", "mem", "gpumem", "threads", "pid", "name", "user", "status"]}


def col_width(key: str, base: int) -> int:
    """At least `base`, and wide enough for the heading plus its sort mark (and, for Status, every status)."""
    width = max(base, Text(T[f"c_{key}"]).cell_len + 2)
    if key == "status":
        width = max(width, *(Text(v).cell_len for k, v in T.items() if k.startswith("st_")))
    return width


def num(s: str, style: str = "") -> Text:
    return Text(s, style=style, justify="right", no_wrap=True)


def gpumem_str(d: dict[int, int]) -> str:
    if not d:
        return ""
    return f"{fmt_bytes(sum(d.values()))} {G.sep} GPU{','.join(str(i) for i in sorted(d))}"


def value_cells(cpu: float, mem: int | None, disk: float | None, gpu_util: float,
                gpu_mem: dict[int, int], cols: list[str]) -> list[Text]:
    cells = []
    for c in cols:
        if c == "cpu":
            cells.append(num(fmt_pct(cpu), heat(cpu, 1, 5, 25)))
        elif c == "mem":
            cells.append(num(fmt_bytes(mem), heat(mem, 512 * MB, 2 * GB, 8 * GB)))
        elif c == "disk":
            cells.append(num(fmt_mbs(disk), heat(disk, MB, 10 * MB, 100 * MB)))
        elif c == "gpu":
            cells.append(num(f"{gpu_util:.0f}%" if gpu_mem or gpu_util else "", heat(gpu_util, 10, 40, 80)))
        elif c == "gpumem":
            cells.append(num(gpumem_str(gpu_mem), heat(sum(gpu_mem.values()), GB, 8 * GB, 32 * GB)))
    return cells


def group_value(g: ProcGroup, col: str):
    return {"name": g.label.lower(), "cpu": g.cpu, "mem": g.mem, "gpu": g.gpu_util,
            "disk": -1.0 if g.disk is None else g.disk, "gpumem": sum(g.gpu_mem.values())}[col]


def proc_value(p: Proc, col: str):
    if col == "name":
        return p.name.lower()
    if col == "cmd":
        return " ".join(p.cmdline).lower()
    return {"cpu": p.cpu, "mem": p.mem or 0, "disk": -1.0 if p.disk is None else p.disk,
            "gpu": p.gpu_util, "gpumem": sum(p.gpu_mem.values()), "pid": p.pid, "status": p.status,
            "user": p.user, "threads": p.threads}[col]


def sort_items(items: list, col: str, desc: bool, value: Callable) -> list:
    items = sorted(items, key=lambda x: value(x, "name"))          # ties: by name
    return sorted(items, key=lambda x: value(x, col), reverse=desc)


def header2(label: str, total: float | None, is_sorted: bool, desc: bool, right: bool) -> Text:
    """A two-line column heading, Windows-style: the machine's total above the column's name."""
    mark = f" {G.sort_desc if desc else G.sort_asc}" if is_sorted else ""
    t = Text(justify="right" if right else "left", no_wrap=True)
    if total is not None:
        t.append(f"{total:.0f}%", "bold")
    t.append("\n" + label + mark)
    return t


def header1(label: str, is_sorted: bool, desc: bool, right: bool) -> Text:
    mark = f" {G.sort_desc if desc else G.sort_asc}" if is_sorted else ""
    return Text(label + mark, justify="right" if right else "left", no_wrap=True)


def info_cols(pairs: list[tuple[str, str]], width: int, tight: bool = False) -> int:
    """How many columns info_grid uses. Normally every column is as wide as the widest cell; `tight`
    lets each column be as wide as its own cells, with a smaller gap, so more fit in a narrow space."""
    sizes = [max(Text(k).cell_len, Text(v).cell_len) for k, v in pairs]
    if not tight:
        return max(1, min(len(pairs), width // (max(sizes, default=10) + 3)))
    for cols in range(len(pairs), 1, -1):
        if sum(max(sizes[j::cols]) for j in range(cols)) + 2 * (cols - 1) <= width:
            return cols
    return 1


def info_grid(pairs: list[tuple[str, str]], width: int, tight: bool = False, cols: int | None = None) -> Table:
    """Label above value, as in the lower half of Windows' Performance pages."""
    cols = cols or info_cols(pairs, width, tight)
    t = Table.grid(padding=(0, 2 if tight else 3))
    for _ in range(cols):
        t.add_column(no_wrap=True)
    cells = [Text.assemble((k, "dim"), "\n", (v, "bold")) for k, v in pairs]
    for i in range(0, len(cells), cols):
        row = cells[i:i + cols]
        t.add_row(*(row + [Text("")] * (cols - len(row))))
    return t


def disk_type(d: DiskDev) -> str:
    kind = "SSD" if d.ssd else "HDD" if d.ssd is False else ""
    return f"{kind} (NVMe)" if d.nvme else kind or G.dash


# NVML's clock event reasons (bits), the most telling first: why a card runs below its top clock
SLOWED = ((0x60, "sl_heat"), (0x80, "sl_brake"), (0x08, "sl_hw"), (0x04, "sl_power"), (0x200, "sl_board"),
          (0x400, "sl_reliability"), (0x01, "sl_idle"), (0x02, "sl_clocks"), (0x100, "sl_display"),
          (0x10, "sl_sync"))


def slowed_by(g: GpuInfo) -> str:
    """The most telling reason for the card's clock to be held down. The driver also sets its power cap
    bit, on and off, on a card that draws a tenth of its limit, so that one counts only near the limit."""
    bits = g.slowed
    if bits is None:
        return G.dash
    if bits & 0x04 and not (g.power and g.power_limit and g.power >= 0.9 * g.power_limit):
        bits &= ~0x04
    return next((T[key] for mask, key in SLOWED if bits & mask), T["h_none"])


# ---------------------------------------------------------------- the app

class TaskManager(App):
    TITLE = T["title"]
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $background; }
    #topbar { height: 1; padding: 0 1; background: $panel; }
    #topbar, #pd-title, #pd-info, .pi-body, Graph, CoreGrid, .gp-title, .gp-info {
        text-wrap: nowrap; text-overflow: ellipsis; }
    #search { display: none; margin: 0 1; }
    #tabs { height: 1fr; }
    #tabs TabPane { padding: 0; height: 1fr; }
    DataTable { height: 1fr; }
    #perf-row { height: 1fr; }
    #perf-list { width: 34; height: 1fr; border-right: solid $panel; }
    #perf-list > ListItem { height: auto; padding: 0 1; margin-bottom: 1; }
    #perf-detail { width: 1fr; height: 1fr; padding: 0 1 0 2; }
    #pd-title { height: 1; margin-bottom: 1; }
    #pd-cores-box { height: 1fr; }
    #pd-info { height: auto; margin-top: 1; }
    #gpu-box { height: 1fr; }
    #gpu-grid { grid-gutter: 1 0; }
    .gpu-panel { padding: 0 1 0 2; }
    .gpu-panel.gp-right { border-left: solid $panel; padding: 0 1; }
    .gp-title { height: 1; margin-bottom: 1; }
    .gp-graph { min-height: 9; }
    .gp-info { height: auto; margin-top: 1; }
    EndTaskScreen, HelpScreen { align: center middle; }
    #dialog { width: 80; max-width: 96%; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    #dlg-title { margin-bottom: 1; }
    #dlg-buttons { height: auto; margin-top: 1; align-horizontal: center; }
    #dlg-buttons Button { margin: 0 1; }
    #help { width: 104; max-width: 96%; height: auto; max-height: 96%; border: round $primary;
            background: $surface; padding: 0 2; }
    """
    BINDINGS = [
        Binding("1", "tab('proc')", T["tab_proc"], show=False),
        Binding("2", "tab('perf')", T["tab_perf"], show=False),
        Binding("3", "tab('details')", T["tab_details"], show=False),
        Binding("4", "tab('gpu')", T["tab_gpu"], show=False),
        Binding("slash", "search", T["b_search"], key_display="/"),
        Binding("delete", "end_task", T["b_end"], key_display="Del"),
        Binding("k", "end_task", T["b_end"], show=False),
        Binding("s", "sort_next", T["b_sort"]),
        Binding("r", "sort_reverse", T["b_reverse"]),
        Binding("l", "cores", T["b_cores"]),
        Binding("z", "zoom", T["b_zoom"]),
        Binding("space", "pause", T["b_pause"], key_display=T["k_space"]),
        Binding("question_mark", "help", T["b_help"], key_display="?"),
        Binding("escape", "escape", show=False),
        Binding("q", "quit", T["b_quit"]),
        Binding("ctrl+c", "quit", show=False, priority=True),
    ]

    def __init__(self, sampler: Sampler, interval: float) -> None:
        super().__init__()
        self.sampler = sampler
        self.interval = interval
        self.has_gpu = bool(sampler.gpus.handles)
        self.has_gpm = bool(getattr(sampler.gpus, "has_gpm", False))
        self.snap: Snapshot | None = None
        self.history = History(math.ceil(max(s for s in SPANS if s) / interval * 1.1) + 10)  # the longest span
        self.paused = False
        self._busy = False
        self._last_error: str | None = None
        self.query_text = ""
        self.sort = {"proc": ("cpu", True), "details": ("cpu", True)}
        self.expanded: set[str] = set()
        self.collapsed: set[str] = set()
        self.perf_sel = "cpu"
        self.show_cores = False
        self.span: float | None = None      # seconds the graphs cover; None: one reading to a column

    # ----------------------------------------------------------- layout

    def perf_keys(self) -> list[str]:
        s = self.sampler
        return ["cpu", "mem"] + [f"disk:{d.name}" for d in s.disks] + [f"nic:{n}" for n in s.nics]

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        yield Input(placeholder=T["search_ph"], id="search", compact=True)
        with TabbedContent(initial="proc", id="tabs"):
            with TabPane(T["tab_proc"], id="proc"):
                yield DataTable(id="proc-table", cursor_type="row", header_height=2)
            with TabPane(T["tab_perf"], id="perf"):
                with Horizontal(id="perf-row"):
                    yield ListView(*[PerfItem(k) for k in self.perf_keys()], id="perf-list")
                    with Vertical(id="perf-detail"):
                        yield Static(id="pd-title")
                        yield Graph(id="pd-graph")
                        with VerticalScroll(id="pd-cores-box"):
                            yield CoreGrid(id="pd-cores")
                        yield Static(id="pd-info")
            with TabPane(T["tab_details"], id="details"):
                yield DataTable(id="details-table", cursor_type="row", fixed_columns=1)
            if self.has_gpu:
                with TabPane(T["tab_gpu"], id="gpu"):
                    yield GpuBox(len(self.sampler.gpus.handles), self.gpu_min_height, id="gpu-box")
        yield Footer()

    async def on_mount(self) -> None:
        box = self.query_one("#pd-cores-box")
        box.display, box.can_focus = False, False   # the list on the left keeps the arrow keys
        self.query_one("#proc-table").focus()
        self.fit_width()
        if CONFIG_ERROR:
            self.notify(CONFIG_ERROR, severity="warning", timeout=12)
        await self.tick(force=True)            # primes every counter: a rate needs two readings
        self.set_timer(0.5, self.tick)         # so the first real numbers come after half a second
        self.set_interval(self.interval, self.tick)

    def on_unmount(self) -> None:
        self.sampler.gpus.close()

    def on_resize(self) -> None:
        self.fit_width()
        self.call_after_refresh(self.render_now)

    def fit_width(self) -> None:
        """The Performance list gives way to the graphs on narrow terminals."""
        self.query_one("#perf-list").styles.width = 34 if self.size.width >= 100 else 26

    @property
    def active_tab(self) -> str:
        try:
            return self.query_one("#tabs", TabbedContent).active
        except Exception:
            return "proc"

    def focus_tab(self) -> None:
        target = {"proc": "#proc-table", "perf": "#perf-list", "details": "#details-table",
                  "gpu": "#gpu-box"}.get(self.active_tab)
        if target:
            self.query_one(target).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("search", "end_task", "sort_next", "sort_reverse"):
            return self.active_tab in ("proc", "details")
        if action == "zoom":
            return self.active_tab in ("perf", "gpu")
        if action == "cores":
            return self.active_tab == "perf"
        if action == "tab" and parameters == ("gpu",):
            return self.has_gpu
        return True

    # ----------------------------------------------------------- reading and drawing

    async def tick(self, force: bool = False) -> None:
        if (self.paused and not force) or self._busy:
            return
        self._busy = True
        try:
            snap = await asyncio.to_thread(self.sampler.sample)
        except Exception as e:  # one bad reading must not end the program
            msg = T["sample_error"].format(err=f"{type(e).__name__}: {e}")
            if msg != self._last_error:
                self.notify(msg, severity="error", timeout=8)
                self._last_error = msg
            return
        finally:
            self._busy = False
        self._last_error = None
        self.snap = snap
        self.history.push(snap)
        self.render_now()

    def render_now(self) -> None:
        if self.snap is None:
            return
        self.render_topbar()
        tab = self.active_tab
        if tab == "proc":
            self.render_proc()
        elif tab == "perf":
            self.render_perf()
        elif tab == "details":
            self.render_details()
        elif tab == "gpu":
            self.render_gpu()

    def render_topbar(self) -> None:
        s = self.snap
        bar = self.query_one("#topbar", Static)
        left = Text(no_wrap=True)
        left.append(T["title"], "bold")
        left.append(f"   CPU {s.cpu:.0f}%   {T['memory']} {fmt_bytes(s.vm.used)} / {fmt_bytes(s.vm.total)}")
        for g in s.gpus:
            left.append(f"   GPU{g.index} {pct0(g.util)}")
        right = (Text(T["paused"], style="bold yellow") if self.paused
                 else Text(time.strftime("%H:%M:%S", time.localtime(s.t)), style="dim"))
        bar.update(two_sided(left, right, bar.size.width or max(20, self.size.width - 2)))

    def fill_table(self, table: DataTable, columns: list[Col], rows: list[tuple[str, list]]) -> None:
        """Rebuild a table, keeping the selected row and the scroll position where they were.

        clear() moves the cursor to the first row, and a cursor that moves scrolls itself into view, so
        every refresh would undo the mouse wheel. Setting the cursor with set_reactive skips that."""
        keep = None
        if table.row_count:
            try:
                keep = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except Exception:
                keep = None
        old_row = table.cursor_coordinate.row
        sx, sy = table.scroll_x, table.scroll_y
        with self.batch_update():
            table.set_reactive(DataTable.cursor_coordinate, Coordinate(0, 0))
            table.clear(columns=True)
            for c in columns:
                table.add_column(c.label, width=c.width, key=c.key)
            for key, cells in rows:
                table.add_row(*cells, key=key)
            if rows:
                idx = next((i for i, (k, _) in enumerate(rows) if k == keep), min(old_row, len(rows) - 1))
                table.set_reactive(DataTable.cursor_coordinate, Coordinate(idx, 0))
            table.scroll_to(x=sx, y=sy, animate=False, immediate=True)

    def proc_matches(self, p: Proc) -> bool:
        q = self.query_text
        return not q or q in p.name.lower() or q in str(p.pid) or q in " ".join(p.cmdline).lower()

    def group_matches(self, g: ProcGroup) -> bool:
        q = self.query_text
        return not q or q in g.label.lower() or any(self.proc_matches(p) for p in g.members)

    def groups_by_key(self) -> dict[str, ProcGroup]:
        return {g.key: g for g in self.snap.groups} if self.snap else {}

    def group_of_pid(self, pid: int) -> ProcGroup | None:
        return next((g for g in self.snap.groups if any(m.pid == pid for m in g.members)), None)

    def render_proc(self) -> None:
        table = self.query_one("#proc-table", DataTable)
        s = self.snap
        col, desc = self.sort["proc"]
        cols = [c for c in PROC_COLS if self.has_gpu or not c.startswith("gpu")]
        width = table.size.width or self.size.width
        widths = {c: col_width(c, PROC_W[c]) for c in cols}
        fixed = sum(widths.values()) + 2 * (len(cols) + 1) + 2
        name_w = max(14, min(72, width - fixed))
        used = sum(g.mem_used or 0 for g in s.gpus)
        total = sum(g.mem_total or 0 for g in s.gpus)
        totals = {"cpu": s.cpu, "mem": s.vm.percent, "disk": max((d.active for d in s.disks), default=0.0),
                  "gpu": max((g.util or 0 for g in s.gpus), default=0.0),
                  "gpumem": used / total * 100 if total else 0.0}
        columns = [Col("name", header2(T["c_name"], None, col == "name", desc, right=False), name_w)]
        columns += [Col(c, header2(T[f"c_{c}"], totals[c], col == c, desc, right=True), widths[c]) for c in cols]
        groups = [g for g in s.groups if self.group_matches(g)]
        rows: list[tuple[str, list]] = []
        for sec in SECTIONS:
            in_sec = [g for g in groups if g.section == sec]
            if not in_sec:
                continue
            folded = sec in self.collapsed
            head = Text(no_wrap=True)
            head.append(G.closed if folded else G.open, "bold")
            head.append(T[f"sec_{sec}"], "bold")
            head.append(f" ({len(in_sec)})", "dim")
            rows.append((f"s:{sec}", [head] + [Text("")] * len(cols)))
            if folded:
                continue
            for g in sort_items(in_sec, col, desc, group_value):
                n = len(g.members)
                open_ = g.key in self.expanded
                name = Text(no_wrap=True, overflow="ellipsis")
                name.append("  ")
                name.append((G.open if open_ else G.closed) if n > 1 else "  ", "dim")
                name.append(g.label)
                if n > 1:
                    name.append(f" ({n})", "dim")
                rows.append((f"g:{g.key}", [name] + value_cells(g.cpu, g.mem, g.disk, g.gpu_util, g.gpu_mem, cols)))
                if open_ and n > 1:
                    for p in sort_items(g.members, col, desc, proc_value):
                        pname = Text(no_wrap=True, overflow="ellipsis")
                        pname.append("      " + p.name)
                        pname.append(f"  {p.pid}", "dim")
                        rows.append((f"p:{p.pid}", [pname] + value_cells(p.cpu, p.mem, p.disk, p.gpu_util,
                                                                         p.gpu_mem, cols)))
        self.fill_table(table, columns, rows)

    def render_details(self) -> None:
        table = self.query_one("#details-table", DataTable)
        s = self.snap
        col, desc = self.sort["details"]
        cols = [c for c in DET_COLS if self.has_gpu or c != "gpumem"]
        width = table.size.width or self.size.width
        name_w = 20
        widths = {c: col_width(c, DET_W[c]) for c in cols if c != "cmd"}
        fixed = name_w + sum(widths.values()) + 2 * (len(cols) + 1) + 2
        columns = [Col("name", header1(T["c_name"], col == "name", desc, right=False), name_w)]
        for c in cols:
            w = max(20, width - fixed) if c == "cmd" else widths[c]
            columns.append(Col(c, header1(T[f"c_{c}"], col == c, desc, right=c in NUMERIC), w))
        procs = [p for p in s.procs.values() if p.ppid != 2 and self.proc_matches(p)]
        rows = []
        for p in sort_items(procs, col, desc, proc_value):
            cells = [Text(p.name, no_wrap=True, overflow="ellipsis")]
            for c in cols:
                if c == "pid":
                    cells.append(num(str(p.pid)))
                elif c == "status":
                    cells.append(Text(T.get(f"st_{p.status}", p.status), no_wrap=True))
                elif c == "user":
                    cells.append(Text(p.user, no_wrap=True, overflow="ellipsis"))
                elif c == "cpu":
                    cells.append(num(fmt_pct(p.cpu), heat(p.cpu, 1, 5, 25)))
                elif c == "mem":
                    cells.append(num(fmt_bytes(p.mem), heat(p.mem, 512 * MB, 2 * GB, 8 * GB)))
                elif c == "threads":
                    cells.append(num(f"{p.threads:,}"))
                elif c == "gpumem":
                    cells.append(num(gpumem_str(p.gpu_mem)))
                elif c == "cmd":
                    cmd = " ".join(p.cmdline) if p.cmdline else f"[{p.name}]"
                    cells.append(Text(cmd, style="dim", no_wrap=True, overflow="ellipsis"))
            rows.append((f"d:{p.pid}", cells))
        self.fill_table(table, columns, rows)

    # ----------------------------------------------------------- the Performance tab

    def _disk(self, key: str) -> tuple[DiskInfo, int]:
        name = key.split(":", 1)[1]
        i = next(i for i, d in enumerate(self.snap.disks) if d.dev.name == name)
        return self.snap.disks[i], i

    def _nic(self, key: str) -> NicInfo:
        name = key.split(":", 1)[1]
        return next(n for n in self.snap.nics if n.name == name)

    def perf_item_text(self, key: str, w: int) -> Text:
        s, h = self.snap, self.history
        if key == "cpu":
            title, value = T["cpu"], f"{s.cpu:.0f}%"
            sub = fmt_ghz(s.freq) + (f"  {s.cpu_temp:.0f} °C" if s.cpu_temp is not None else "")
            spark = sparkline(h.get("cpu"), w, 100, COLORS["cpu"])
        elif key == "mem":
            title, value = T["memory"], f"{s.vm.percent:.0f}%"
            sub = f"{fmt_bytes(s.vm.used)} / {fmt_bytes(s.vm.total)}"
            spark = sparkline(h.get("mem"), w, 100, COLORS["mem"])
        elif key.startswith("disk:"):
            d, i = self._disk(key)
            title, value = T["disk_n"].format(n=i), f"{d.active:.0f}%"
            sub = disk_type(d.dev) + (f"  {d.temp:.0f} °C" if d.temp is not None else f"  {d.dev.name}")
            spark = sparkline(h.get(f"disk.{d.dev.name}.active"), w, 100, COLORS["disk"])
        else:
            n = self._nic(key)
            title, value = (T["wifi"] if n.wifi else T["ethernet"]), ""
            sub = f"{G.down} {fmt_rate(n.recv)}  {G.up} {fmt_rate(n.sent)}"
            both = [a + b for a, b in zip(h.get(f"nic.{n.name}.recv"), h.get(f"nic.{n.name}.sent"))]
            spark = sparkline(both, w, None, COLORS["net"])
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append_text(two_sided(Text(title, style="bold"), Text(value, style="bold"), w))
        text.append("\n")
        text.append(sub, "dim")
        text.append("\n")
        text.append_text(spark)
        return text

    def perf_title(self, key: str) -> tuple[str, str]:
        s = self.snap
        if key == "cpu":
            return T["cpu"], self.sampler.cpu_model
        if key == "mem":
            return T["memory"], fmt_bytes(s.vm.total)
        if key.startswith("disk:"):
            d, i = self._disk(key)
            return f"{T['disk_n'].format(n=i)} ({d.dev.name})", d.dev.model
        n = self._nic(key)
        return (T["wifi"] if n.wifi else T["ethernet"]), n.name

    def perf_series(self, key: str) -> list[Series]:
        s, h = self.snap, self.history
        if key == "cpu":
            return [Series(T["g_util"], h.get("cpu"), 100, "100%", COLORS["cpu"])]
        if key == "mem":
            return [Series(T["g_mem"], h.get("mem"), 100, fmt_bytes(s.vm.total), COLORS["mem"])]
        if key.startswith("disk:"):
            d, _ = self._disk(key)
            pre = f"disk.{d.dev.name}"
            return [Series(T["g_active"], h.get(pre + ".active"), 100, "100%", COLORS["disk"]),
                    Series(T["g_transfer"], [], None, None, "", parts=[
                        (T["lg_read"], h.get(pre + ".read"), COLORS["disk"]),
                        (T["lg_write"], h.get(pre + ".write"), COLORS["disk2"])])]
        n = self._nic(key)
        return [Series(T["g_throughput"], [], None, None, "", parts=[
            (T["lg_recv"], h.get(f"nic.{n.name}.recv"), COLORS["net"]),
            (T["lg_send"], h.get(f"nic.{n.name}.sent"), COLORS["net2"])])]

    def perf_info(self, key: str, width: int):
        s = self.snap
        if key == "cpu":
            pairs = [(T["i_util"], f"{s.cpu:.0f}%"), (T["i_speed"], fmt_ghz(s.freq)),
                     (T["i_maxspeed"], fmt_ghz(s.freq_max)), (T["i_procs"], f"{s.nprocs:,}"),
                     (T["i_threads"], f"{s.nthreads:,}"), (T["i_uptime"], fmt_uptime(s.uptime)),
                     (T["i_cores"], str(self.sampler.ncores)), (T["i_logical"], str(self.sampler.ncpu)),
                     (T["i_load"], "  ".join(f"{x:.2f}" for x in s.load))]
            if s.cpu_temp is not None:
                pairs.insert(3, (T["i_cpu_temp"], f"{s.cpu_temp:.0f} °C"))
            if self.show_cores:
                return self.busy_cores(width)
            return RichGroup(info_grid(pairs, width), Text(""), Text(T["cores_hint"], style="dim"))
        if key == "mem":
            vm, sw = s.vm, s.swap
            cache = getattr(vm, "buffers", 0) + getattr(vm, "cached", 0)
            bar = Text(no_wrap=True)
            parts = [(vm.used, G.bar[0], COLORS["mem"]), (cache, G.bar[1], dim_hex(COLORS["mem"])),
                     (vm.free, G.bar[2], "grey50")]
            done = 0
            for i, (b, ch, st) in enumerate(parts):
                n = width - done if i == len(parts) - 1 else round(b / vm.total * width)
                n = max(0, min(n, width - done))
                bar.append(ch * n, st)
                done += n
            legend = Text.assemble((G.bar[0], COLORS["mem"]), f" {T['i_inuse']}   ",
                                   (G.bar[1], dim_hex(COLORS["mem"])), f" {T['i_cached']}   ",
                                   (G.bar[2], "grey50"), f" {T['i_free']}")
            pairs = [(T["i_inuse"], f"{fmt_bytes(vm.used)} ({vm.percent:.0f}%)"), (T["i_avail"], fmt_bytes(vm.available)),
                     (T["i_cached"], fmt_bytes(cache)), (T["i_shared"], fmt_bytes(getattr(vm, "shared", 0))),
                     (T["i_swap"], f"{fmt_bytes(sw.used)} / {fmt_bytes(sw.total)}"), (T["i_total"], fmt_bytes(vm.total))]
            return RichGroup(Text(T["i_comp"], style="dim"), bar, legend, Text(""), info_grid(pairs, width))
        if key.startswith("disk:"):
            d, _ = self._disk(key)
            pairs = [(T["i_active"], f"{d.active:.0f}%"),
                     (T["i_resp"], G.dash if d.resp_ms is None else f"{d.resp_ms:.1f} ms"),
                     (T["i_read"], fmt_rate(d.read)), (T["i_write"], fmt_rate(d.write))]
            if d.temp is not None:
                pairs.append((T["i_temp"], f"{d.temp:.0f} °C"))
            pairs += [(T["i_capacity"], fmt_bytes(d.dev.size)), (T["i_type"], disk_type(d.dev))]
            return RichGroup(info_grid(pairs, width), Text(""), self.disk_extra(d, width))
        n = self._nic(key)
        pairs = [(T["i_recv"], fmt_rate(n.recv)), (T["i_sent"], fmt_rate(n.sent)),
                 (T["i_link"], fmt_link(n.speed) if n.up else T["i_down"]), (T["i_adapter"], n.name),
                 (T["i_ipv4"], ", ".join(n.ipv4) or G.dash), (T["i_ipv6"], n.ipv6[0] if n.ipv6 else G.dash),
                 (T["i_total_recv"], fmt_bytes(n.total_recv)), (T["i_total_sent"], fmt_bytes(n.total_sent))]
        return info_grid(pairs, width)

    def disk_extra(self, d: DiskInfo, width: int):
        """Under a disk's numbers: how full each partition is, and the drive's health."""
        space = Table.grid(padding=(0, 2))
        for justify in ("left", "right", "left", "right"):
            space.add_column(no_wrap=True, justify=justify)
        for mount, used, total, pct in d.space:
            color = COLORS["disk"] if pct < 70 else HEAT[1] if pct < 90 else HEAT[2]
            space.add_row(Text(mount, style="bold"), Text(f"{fmt_bytes(used)} / {fmt_bytes(total)}"),
                          stacked_bar([(pct, color)], 12), Text(f"{pct:.0f}%"))
        left = RichGroup(Text(T["i_space"], style="dim"), space) if d.space else Text("")
        if not d.dev.nvme:
            return left
        h = d.health
        if h is None:
            right = Text(T["h_na"], style="dim")
        else:
            pairs = [(T["h_used"], f"{h.get('percent_used', 0)}%"), (T["h_spare"], f"{h.get('avail_spare', 0)}%"),
                     (T["h_written"], fmt_bytes(h.get("total_data_written"))),
                     (T["h_read"], fmt_bytes(h.get("total_data_read"))),
                     (T["h_hours"], f"{h.get('power_on_hours', 0):,} h"),
                     (T["h_unsafe"], f"{h.get('unsafe_shutdowns', 0):,}"),
                     (T["h_media"], f"{h.get('media_errors', 0):,}"),
                     (T["h_warn"], ", ".join(h.get("warnings") or []) or T["h_none"])]
            grid = Table.grid(padding=(0, 2))
            for _ in range(2):
                grid.add_column(no_wrap=True)
                grid.add_column(no_wrap=True)
            for i in range(0, len(pairs), 2):
                row = []
                for label, value in pairs[i:i + 2]:
                    bad = (label == T["h_warn"] and value != T["h_none"]) or (label == T["h_media"] and value != "0")
                    row += [Text(label, style="dim"), Text(value, style="bold " + HEAT[2] if bad else "bold")]
                grid.add_row(*row)
            right = RichGroup(Text(T["i_health"], style="dim"), grid)
        if width < 100:
            return RichGroup(left, Text(""), right)
        both = Table.grid(padding=(0, 6))
        both.add_column(no_wrap=True)
        both.add_column(no_wrap=True)
        both.add_row(left, right)
        return both

    def busy_cores(self, width: int) -> RichGroup:
        """Under the per-core view: the busiest logical processors, and the program each one runs."""
        s = self.snap
        rows = []
        for i, t in enumerate(s.per_cpu_times):
            user, kern, _ = CoreGrid.split(t)
            if user + kern >= 5:
                rows.append((min(100.0, user + kern), i, user, kern))
        rows.sort(key=lambda r: r[0], reverse=True)
        head = Text(T["busy_title"], style="dim")
        if not rows:
            return RichGroup(head, Text(T["busy_none"]), Text(""), Text(T["cores_back"], style="dim"))
        by_pid = {m.pid: g for g in s.groups for m in g.members}
        wide = width >= 70                      # room for the clock and the process details
        table = Table.grid(padding=(0, 2))
        for justify in ("left", "right", "right", "left", "left") if wide else ("left", "right", "left"):
            table.add_column(no_wrap=True, justify=justify)
        dash = False
        for busy, i, user, kern in rows[:6]:
            top = s.core_top.get(i)
            ghz = s.per_cpu_freq[i] / 1000 if i < len(s.per_cpu_freq) else 0.0
            if top:
                _, pid, thread = top
                grp, p = by_pid.get(pid), s.procs.get(pid)
                prog = grp.label if grp else (p.name if p else "?")
                name = p.name if p else "?"
                detail = (f"{name} / {thread}" if thread != name else name) + f"  PID {pid}"
            elif kern > user:
                prog, detail = T["busy_kernel"], ""
            else:
                prog, detail, dash = G.dash, "", True
            cells = [Text(f"CPU {i}", style="bold"), Text(f"{busy:.0f}%", style=heat(busy, 10, 40, 80))]
            if wide:
                cells.append(Text(f"{ghz:.2f} GHz" if ghz else G.dash, style="dim"))
            cells.append(Text(prog))
            if wide:
                cells.append(Text(detail, style="dim"))
            table.add_row(*cells)
        foot = [Text(T["busy_note"], style="dim")] if dash else []
        return RichGroup(head, table, *foot, Text(""), Text(T["cores_back"], style="dim"))

    def render_perf(self) -> None:
        for item in self.query(PerfItem):
            body = item.query_one(Static)
            body.update(self.perf_item_text(item.key, body.size.width or 30))
        key = self.perf_sel
        title, model = self.perf_title(key)
        head = self.query_one("#pd-title", Static)
        head.update(two_sided(Text(title, style="bold"), Text(model, style="dim"), head.size.width or 60, cut="right"))
        graph, box = self.query_one("#pd-graph", Graph), self.query_one("#pd-cores-box")
        use_cores = key == "cpu" and self.show_cores
        graph.display, box.display = not use_cores, use_cores
        if use_cores:
            self.query_one("#pd-cores", CoreGrid).set_data(self.sampler.cpu_groups, self.snap.per_cpu_times,
                                                           self.snap.per_cpu_freq)
        else:
            graph.set_series(self.perf_series(key), self.interval, self.span, self.history.count)
        info = self.query_one("#pd-info", Static)
        info.update(self.perf_info(key, info.size.width or 80))

    # ----------------------------------------------------------- the GPU tab

    PROCS_MAX = 8          # processes listed under a card at most

    @property
    def graph_min(self) -> int:
        """A card's graphs at their smallest: each a label and two rows, then the time axis."""
        return 3 * (3 if self.has_gpm else 2) + 1

    # A card's numbers in fours: what it is doing; its state; clocks and PCIe. Memory sits under memory
    # bandwidth, the two widest in one column, so four columns fit in half of a 130-wide window.
    GPU_KEYS = ("util", "sm", "tensor", "bw", "temp", "power", "fan", "vram", "sm_clock", "memclk", "tx", "rx",
                "slowed", "driver")
    GPU_KEYS_NO_GPM = ("util", "vram", "temp", "power", "fan", "sm_clock", "memclk", "slowed", "driver")
    GPU_LEAST = ("driver", "memclk", "fan")   # left out first when a card's panel is narrow

    def gpu_pairs(self, g: GpuInfo, keys: tuple[str, ...], widest: bool = False) -> list[tuple[str, str]]:
        """A card's numbers, those in `keys`. `widest`: each value as wide as it can get, to size the grid
        by, so the grid keeps its shape while the numbers change."""
        if widest:
            g = replace(g, util=100.0, mem_used=1000 * MB, temp=100.0, power=g.power_limit or 1000.0, fan=100.0,
                        clock_sm=99_999, clock_mem=99_999, sm=100.0, tensor=100.0, mem_bw=100.0,
                        pcie_tx=1000 * MB, pcie_rx=1000 * MB)
        v = {"util": pct0(g.util), "sm": pct0(g.sm), "tensor": pct0(g.tensor), "bw": pct0(g.mem_bw),
             "vram": f"{fmt_bytes(g.mem_used)} / {fmt_bytes(g.mem_total)}",
             "temp": G.dash if g.temp is None else f"{g.temp:.0f} °C",
             "power": G.dash if g.power is None else
             f"{g.power:.0f} / {g.power_limit:.0f} W" if g.power_limit else f"{g.power:.0f} W",
             "fan": pct0(g.fan), "sm_clock": G.dash if g.clock_sm is None else f"{g.clock_sm:,} MHz",
             "memclk": G.dash if g.clock_mem is None else f"{g.clock_mem:,} MHz",
             "tx": fmt_rate(g.pcie_tx), "rx": fmt_rate(g.pcie_rx),
             "slowed": max((T[key] for _, key in SLOWED), key=lambda s: Text(s).cell_len) if widest
             else slowed_by(g), "driver": self.sampler.gpus.driver or G.dash}
        label = {"util": "i_util", "sm": "i_sm_busy", "tensor": "i_tensor", "bw": "i_bw", "vram": "i_vram",
                 "temp": "i_temp", "power": "i_power", "fan": "i_fan", "sm_clock": "i_sm", "memclk": "i_memclk",
                 "tx": "i_pcie_tx", "rx": "i_pcie_rx", "slowed": "i_slowed", "driver": "i_driver"}
        return [(T[label[k]], v[k]) for k in keys]

    def gpu_series(self, g: GpuInfo) -> list[Series]:
        h = self.history
        bw = [Series(T["g_bw"], h.get(f"gpu{g.index}.bw"), 100, "100%", COLORS["gpu"])] if self.has_gpm else []
        return ([Series(T["g_util"], h.get(f"gpu{g.index}.util"), 100, "100%", COLORS["gpu"])] + bw
                + [Series(T["g_gpumem"], h.get(f"gpu{g.index}.mem"), g.mem_total or 1,
                          fmt_bytes(g.mem_total), COLORS["gpu"])])

    @staticmethod
    def gpu_shown(g: GpuInfo, lines: int) -> tuple[list[tuple[int, int]], int]:
        """The processes on a card that fit in `lines` lines, biggest first, and how many are left out
        (then the last line says so)."""
        on_card = sorted(g.procs.items(), key=lambda kv: kv[1], reverse=True)
        if len(on_card) <= lines:
            return on_card, 0
        keep = max(0, lines - 1)
        return on_card[:keep], len(on_card) - keep

    @staticmethod
    def gpu_procs(shown: list[tuple[int, int]], left_out: int, lines: int, names: dict[int, tuple[str, str]],
                  widths: tuple[int, int | None, int]) -> list:
        """A card's processes in exactly `lines` lines, blank ones filling up the rest. `names`: pid -> (its
        app, its own name). `widths`: of the app, name and PID columns, the same on every card so the
        columns line up; a name width of None leaves the names out."""
        label_w, name_w, pid_w = widths
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True, width=label_w)
        if name_w is not None:
            table.add_column(no_wrap=True, width=name_w)
        table.add_column(no_wrap=True, justify="right", width=pid_w)
        table.add_column(no_wrap=True, justify="right", width=7)   # fmt_bytes is at most 7 wide
        for pid, used in shown:
            label, name = names[pid]
            cells = [Text(label, style="bold")] + ([Text(name, style="dim")] if name_w is not None else [])
            table.add_row(*cells, Text(str(pid), style="dim"), Text(fmt_bytes(used)))
        out: list = [table] if shown else []
        if left_out:
            out.append(Text(T["end_more"].format(n=left_out), style="dim"))
        used_lines = len(shown) + (1 if left_out else 0)
        return out + [Text("")] * max(0, lines - used_lines)

    def gpu_grid(self, width: int) -> tuple[tuple[str, ...], int, int]:
        """(which numbers, columns, lines) of the cards' numbers at this width, one shape for every card.
        A narrow panel leaves out the least telling numbers until they fit in four rows."""
        keys = self.GPU_KEYS if self.has_gpm else self.GPU_KEYS_NO_GPM
        cards = self.snap.gpus if self.snap else []
        if not cards:
            return keys, 2, 2 * math.ceil(len(keys) / 2)
        least = list(self.GPU_LEAST)
        while True:
            cols = min(info_cols(self.gpu_pairs(g, keys, widest=True), width, tight=True) for g in cards)
            rows = math.ceil(len(keys) / cols)
            if rows <= 4 or not least:
                return keys, cols, 2 * rows
            drop = least.pop(0)
            keys = tuple(k for k in keys if k != drop)

    def gpu_min_height(self, width: int) -> int:
        """The least height a card's panel needs at this width: its title, the graph at its smallest, the
        numbers, and the first two processes."""
        return 2 + self.graph_min + 1 + self.gpu_grid(width)[2] + 2 + 2

    def render_gpu(self) -> None:
        """Every card at once. All the panels are drawn alike, the numbers in the same grid and the process
        lists padded to the same length, so their graphs are the same height and line up side by side."""
        self.query_one("#gpu-box", GpuBox).place_cards()   # the least height can change once cards are read
        cards = {g.index: g for g in self.snap.gpus}
        panels = [(p, cards[p.index]) for p in self.query(GpuPanel) if p.index in cards]
        if not panels:
            return
        width = panels[0][0].query_one(".gp-title", Static).size.width or 40
        height = panels[0][0].size.height
        keys, cols, grid_lines = self.gpu_grid(width)
        room = height - 2 - self.graph_min - 1 - grid_lines - 2   # title, graphs, blank, numbers, blank, heading
        most = max(len(g.procs) for _, g in panels)
        lines = min(most, self.PROCS_MAX, max(1, room))
        shown = {g.index: self.gpu_shown(g, lines) for _, g in panels}
        names = {}
        for rows, _ in shown.values():
            for pid, _ in rows:
                p, grp = self.snap.procs.get(pid), self.group_of_pid(pid)
                names[pid] = (grp.label if grp else G.dash, p.name if p else "?")
        label_w = max((Text(a).cell_len for a, _ in names.values()), default=1)
        name_w: int | None = max((Text(b).cell_len for _, b in names.values()), default=1)
        pid_w = max((len(str(pid)) for pid in names), default=1)
        fixed = pid_w + 7 + 2 * 2                  # the PID and memory columns, and the gaps before them
        if width - fixed - name_w - 2 < min(label_w, 16):
            name_w = None                          # the app's name comes first; the process's own if there is room
        label_w = max(1, min(label_w, width - fixed - (0 if name_w is None else name_w + 2)))
        for panel, g in panels:
            title, name = T["gpu_n"].format(n=g.index), g.name
            if len(title) + 1 + len(name) > width and name.startswith("NVIDIA "):
                name = name[len("NVIDIA "):]       # every card here is NVIDIA's: short of room, that goes first
            head = panel.query_one(".gp-title", Static)
            head.update(two_sided(Text(title, style="bold"), Text(name, style="dim"), width, cut="right"))
            panel.query_one(Graph).set_series(self.gpu_series(g), self.interval, self.span, self.history.count)
            heading = Text(T["i_gpu_procs"] + ("" if g.procs else f"：{T['i_none']}" if ARGS.lang == "zh"
                                               else f": {T['i_none']}"), style="dim")
            panel.query_one(".gp-info", Static).update(RichGroup(
                info_grid(self.gpu_pairs(g, keys), width, tight=True, cols=cols), Text(""), heading,
                *self.gpu_procs(*shown[g.index], lines, names, (label_w, name_w, pid_w))))

    # ----------------------------------------------------------- events

    @on(TabbedContent.TabActivated, "#tabs")
    def _tab_changed(self) -> None:
        self.refresh_bindings()
        self.focus_tab()
        self.call_after_refresh(self.render_now)

    @on(ListView.Highlighted, "#perf-list")
    def _perf_pick(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, PerfItem):
            self.perf_sel = event.item.key
            if self.snap and self.active_tab == "perf":
                self.render_perf()

    @on(DataTable.RowSelected, "#proc-table")
    def _proc_toggle(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if key.startswith("s:"):
            self.collapsed ^= {key[2:]}
        elif key.startswith("g:"):
            g = self.groups_by_key().get(key[2:])
            if not g or len(g.members) < 2:
                return
            self.expanded ^= {g.key}
        else:
            return
        self.render_proc()

    @on(DataTable.HeaderSelected)
    def _header_click(self, event: DataTable.HeaderSelected) -> None:
        tab = "proc" if event.control.id == "proc-table" else "details"
        col = event.column_key.value
        cur, desc = self.sort[tab]
        self.sort[tab] = (col, not desc) if col == cur else (col, col in BIGGEST_FIRST)
        self.render_now()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.query_text = event.value.strip().lower()
        self.render_now()

    @on(Input.Submitted, "#search")
    def _search_done(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            event.input.display = False
        self.focus_tab()

    # ----------------------------------------------------------- actions

    def action_tab(self, name: str) -> None:
        self.query_one("#tabs", TabbedContent).active = name

    def action_search(self) -> None:
        inp = self.query_one("#search", Input)
        inp.display = True
        inp.focus()

    def action_escape(self) -> None:
        inp = self.query_one("#search", Input)
        if inp.display or self.query_text:
            inp.value = ""
            inp.display = False
            self.query_text = ""
            self.focus_tab()
            self.render_now()

    def action_sort_next(self) -> None:
        tab = self.active_tab
        if tab not in self.sort:
            return
        order = [c for c in SORT_ORDER[tab] if self.has_gpu or not c.startswith("gpu")]
        cur = self.sort[tab][0]
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
        self.sort[tab] = (nxt, nxt in BIGGEST_FIRST)
        self.render_now()

    def action_sort_reverse(self) -> None:
        tab = self.active_tab
        if tab in self.sort:
            col, desc = self.sort[tab]
            self.sort[tab] = (col, not desc)
            self.render_now()

    def action_zoom(self) -> None:
        self.span = SPANS[(SPANS.index(self.span) + 1) % len(SPANS)]
        if self.snap:
            self.render_now()

    def action_cores(self) -> None:
        self.show_cores = not self.show_cores
        if self.snap and self.active_tab == "perf":
            self.render_perf()

    def action_pause(self) -> None:
        self.paused = not self.paused
        if self.snap:
            self.render_topbar()

    def action_help(self) -> None:
        projects = ", ".join(p.name for p in PROJECTS) or (G.dash if CONFIG_ERROR else T["cfg_none"])
        tabs = [T[k] for k in ("tab_proc", "tab_perf", "tab_details", "tab_gpu")[:4 if self.has_gpu else 3]]
        self.push_screen(HelpScreen(T["help"].format(
            ncpu=self.sampler.ncpu, config=tilde(CONFIG_FILE), projects=projects,
            tab_keys="  ".join(t.split(" ", 1)[0] for t in tabs),
            tab_names=("／" if ARGS.lang == "zh" else " / ").join(t.split(" ", 1)[1] for t in tabs))))

    def action_end_task(self) -> None:
        tab = self.active_tab
        if tab not in ("proc", "details") or not self.snap:
            return
        table = self.query_one("#proc-table" if tab == "proc" else "#details-table", DataTable)
        if not table.row_count:
            return
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or ""
        except Exception:
            return
        if key.startswith("g:"):
            g = self.groups_by_key().get(key[2:])
            if g is None:
                return
            if g.key == "kernel":
                self.notify(T["no_kernel"], severity="warning")
                return
            roots = g.roots
            title, targets = g.label, roots + [m for m in g.members if m not in roots]
            note = end_note(g.key)
        elif key[:2] in ("p:", "d:"):
            p = self.snap.procs.get(int(key[2:]))
            if p is None:
                return
            if p.kernel:
                self.notify(T["no_kernel"], severity="warning")
                return
            grp = self.group_of_pid(p.pid)
            title, targets, note = p.name, [p], end_note(grp.key) if grp else None
        else:
            self.notify(T["nothing"])
            return
        if len(targets) == 1 and targets[0].pid == 1:
            self.notify(T["no_init"], severity="warning")
            return
        me = os.getpid()
        if len(targets) == 1 and targets[0].pid == me:
            self.notify(T["no_self"], severity="warning")
            return
        targets = [p for p in targets if p.pid > 1 and p.pid != me]
        if not targets:
            self.notify(T["nothing"])
            return
        self.push_screen(EndTaskScreen(T["end_title"].format(name=title), self.end_body(targets, note)),
                         lambda choice: self.do_end(targets, choice))

    def end_body(self, targets: list[Proc], note: str | None) -> Text:
        body = Text()
        body.append(T["end_count"].format(n=len(targets)) + "\n")
        for p in targets[:6]:
            body.append(f"  {p.pid:>7}  ", "dim")
            body.append(p.name)
            body.append(f"  ({p.user})\n", "dim")
        if len(targets) > 6:
            body.append("  " + T["end_more"].format(n=len(targets) - 6) + "\n", "dim")
        others = [p for p in targets if p.uid != os.getuid()]
        if others and os.geteuid() != 0:
            users = ", ".join(sorted({p.user for p in others}))
            body.append("\n" + T["end_others"].format(n=len(others), users=users) + "\n", "yellow")
        if note:
            body.append("\n" + note + "\n", "cyan")
        body.append("\n" + T["end_explain"], "dim")
        return body

    def do_end(self, targets: list[Proc], choice: str | None) -> None:
        if choice not in ("term", "kill"):
            return
        sig = signal.SIGTERM if choice == "term" else signal.SIGKILL
        sent = gone = denied = 0
        denied_users: set[str] = set()
        for p in targets:
            try:
                if read_stat(p.pid)[5] != p.ct:
                    gone += 1  # the pid now belongs to another process
                    continue
                os.kill(p.pid, sig)
                sent += 1
            except (ProcessLookupError, FileNotFoundError):
                gone += 1
            except PermissionError:
                denied += 1
                denied_users.add(p.user)
        msgs = []
        if sent:
            msgs.append(T["sent"].format(sig=sig.name, n=sent))
        if denied:
            msgs.append(T["denied"].format(n=denied, users=", ".join(sorted(denied_users))))
        if gone:
            msgs.append(T["gone"].format(n=gone))
        self.notify("\n".join(msgs), severity="warning" if denied else "information")
        self.set_timer(0.4, lambda: self.run_worker(self.tick(force=True)))


def main() -> None:
    TaskManager(Sampler(ARGS.lang), ARGS.interval).run()


if __name__ == "__main__":
    main()
