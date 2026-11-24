#!/usr/bin/env python3
"""Draws the README's screenshots (docs/*.png) from a made-up machine, so no real process or path shows.

    venv/bin/pip install pillow
    venv/bin/python docs/make_screenshots.py

The app runs headless on a fake sampler (a 16-core AMD desktop with two NVIDIA cards, an NVMe SSD and a
backup HDD), is fed two minutes of readings, then each page is painted cell by cell with the DejaVu
fonts. Block characters are drawn as shapes, so the pictures line up whatever fonts the reader has.
"""

import asyncio
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS

from PIL import Image, ImageDraw, ImageFont
from rich.cells import cell_len
from rich.style import Style

HERE = Path(__file__).resolve().parent
COLS, ROWS = 120, 36
STEPS = 150                      # readings fed in before the first picture: the graphs need history
SCALE = 2                        # drawn at twice the size, so it stays sharp on high-density screens
FONTS = Path("/usr/share/fonts/truetype/dejavu")
BG, FG = (0x12, 0x12, 0x12), (0xE0, 0xE0, 0xE0)
GB, MB = 1 << 30, 1 << 20

# A project of the made-up user, so the Processes tab shows groups.toml at work.
GROUPS_TOML = """
[[project]]
name = "Blog"
dir = "/home/alex/blog"
  [project.scripts."serve.py"]
  name = "Blog server"
"""
_cfg = tempfile.mkdtemp()
os.makedirs(f"{_cfg}/taskmgr")
Path(f"{_cfg}/taskmgr/groups.toml").write_text(GROUPS_TOML)
os.environ["XDG_CONFIG_HOME"] = _cfg  # read when taskmgr is imported
os.environ["TZ"] = "UTC"              # the clock in the corner: the same wherever this runs
time.tzset()
sys.path.insert(0, str(HERE.parent))
import taskmgr as tm  # noqa: E402

NCPU, ME = 32, 1000
# Busy threads: (pid, thread name, logical CPU, % of that CPU at full load)
HOT = [(3300, "python3", 3, 96), (3300, "python3", 4, 93), (3300, "python3", 10, 91), (3300, "python3", 12, 88),
       (3561, "VLLM::EngineCor", 7, 99), (3500, "python3", 25, 82), (2400, "firefox", 1, 34),
       (1800, "gnome-shell", 17, 27)]


class Fake:
    """The made-up processes: (pid, ppid, name, uid, user, cmdline, threads, % of one CPU, MB, cwd)."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        root = lambda *a, **k: self.add(*a, uid=0, user="root", **k)  # noqa: E731
        self.add(2, 0, "kthreadd", [], uid=0, user="root")
        names = ["rcu_preempt", "kcompactd0", "khugepaged", "kswapd0", "nvme-wq", "irq/120-nvidia", "jbd2/nvme0n1p2"]
        names += [f"{n}/{c}" for c in range(NCPU) for n in ("ksoftirqd", "migration", "kworker")][:70]
        for i, n in enumerate(names):
            self.add(10 + i, 2, n, [], uid=0, user="root", cpu=0.4 if n.startswith(("kworker", "irq")) else 0.05)
        root(1, 0, "systemd", ["/sbin/init", "splash"], threads=1, cpu=0.3, mb=14)
        for pid, name, cmd, cpu, mb in [(412, "systemd-journal", ["/usr/lib/systemd/systemd-journald"], 0.2, 48),
                                        (455, "systemd-udevd", ["/usr/lib/systemd/systemd-udevd"], 0.1, 9),
                                        (921, "NetworkManager", ["/usr/sbin/NetworkManager", "--no-daemon"], 0.1, 21),
                                        (955, "accounts-daemon", ["/usr/libexec/accounts-daemon"], 0.0, 8),
                                        (1003, "cupsd", ["/usr/sbin/cupsd", "-l"], 0.0, 6),
                                        (1010, "sshd", ["sshd: /usr/sbin/sshd -D [listener]"], 0.0, 7),
                                        (1102, "gdm3", ["/usr/sbin/gdm3"], 0.0, 6)]:
            root(pid, 1, name, cmd, cpu=cpu, mb=mb)
        self.add(873, 1, "systemd-resolve", ["/usr/lib/systemd/systemd-resolved"], uid=991, user="systemd-resolve", mb=12)
        self.add(930, 1, "polkitd", ["/usr/lib/polkit-1/polkitd", "--no-debug"], uid=995, user="polkitd", mb=9)
        pg = ["/usr/lib/postgresql/16/bin/postgres", "-D", "/var/lib/postgresql/16/main"]
        self.add(1150, 1, "postgres", pg, uid=129, user="postgres", cpu=0.1, mb=31)
        for i, role in enumerate(["checkpointer", "background writer", "walwriter", "autovacuum launcher"]):
            self.add(1160 + i, 1150, "postgres", [f"postgres: 16/main: {role}"], uid=129, user="postgres", mb=4)
        # the user's session
        self.add(1700, 1, "systemd", ["/usr/lib/systemd/systemd", "--user"], cpu=0.1, mb=12)
        for pid, name, cmd, cpu, mb in [(1720, "pipewire", ["/usr/bin/pipewire"], 0.4, 18),
                                        (1721, "wireplumber", ["/usr/bin/wireplumber"], 0.1, 22),
                                        (1722, "pipewire-pulse", ["/usr/bin/pipewire-pulse"], 0.2, 14),
                                        (1740, "dbus-daemon", ["/usr/bin/dbus-daemon", "--session"], 0.1, 6),
                                        (1990, "gsd-power", ["/usr/libexec/gsd-power"], 0.0, 26),
                                        (1991, "gsd-media-keys", ["/usr/libexec/gsd-media-keys"], 0.0, 27),
                                        (1992, "gsd-color", ["/usr/libexec/gsd-color"], 0.0, 24),
                                        (1993, "gsd-xsettings", ["/usr/libexec/gsd-xsettings"], 0.0, 63),
                                        (2010, "tracker-miner-f", ["/usr/libexec/tracker-miner-fs-3"], 0.3, 41),
                                        (2020, "evolution-sourc", ["/usr/libexec/evolution-source-registry"], 0.0, 30),
                                        (2030, "xdg-desktop-por", ["/usr/libexec/xdg-desktop-portal"], 0.0, 19),
                                        (2040, "ibus-daemon", ["/usr/bin/ibus-daemon", "--panel", "disable"], 0.2, 15),
                                        (2050, "gvfsd", ["/usr/libexec/gvfsd"], 0.0, 8)]:
            self.add(pid, 1700, name, cmd, cpu=cpu, mb=mb, threads=4)
        self.add(1800, 1700, "gnome-shell", ["/usr/bin/gnome-shell"], cpu=27, mb=412, threads=31)
        self.add(1850, 1800, "Xwayland", ["/usr/bin/Xwayland", ":0", "-rootless"], cpu=2.1, mb=96, threads=9)
        self.add(1860, 1800, "gjs", ["/usr/bin/gjs", "-m", "/usr/share/gnome-shell/org.gnome.Shell.Notifications"], mb=31)
        ff = "/usr/lib/firefox/firefox"
        self.add(2400, 1800, "firefox", [ff], cpu=14, mb=640, threads=96)
        for i, (name, cpu, mb) in enumerate([("Isolated Web Co", 9, 420), ("Isolated Web Co", 3, 236),
                                             ("Isolated Web Co", 0.6, 148), ("Web Content", 0.1, 52),
                                             ("WebExtensions", 0.4, 151), ("Socket Process", 0.2, 22),
                                             ("RDD Process", 0.1, 31), ("Utility Process", 0.0, 28)]):
            self.add(2410 + i, 2400, name, [ff, "-contentproc"], cpu=cpu, mb=mb, threads=24)
        code = "/usr/share/code/code"
        self.add(2600, 1800, "code", [code], cpu=4, mb=388, threads=34)
        for i, (kind, cpu, mb) in enumerate([("zygote", 0, 61), ("gpu-process", 1.4, 182), ("renderer", 3.2, 517),
                                             ("utility", 1.1, 233), ("utility", 0.2, 96)]):
            self.add(2610 + i, 2600, "code", [code, f"--type={kind}"], cpu=cpu, mb=mb, threads=18)
        self.add(2800, 1700, "nautilus", ["/usr/bin/nautilus", "--gapplication-service"], cpu=0.1, mb=128, threads=8)
        # a terminal, and what was typed in it
        self.add(1900, 1700, "gnome-terminal-", ["/usr/libexec/gnome-terminal-server"], cpu=1.3, mb=88, threads=6)
        for i in range(4):
            self.add(1950 + i, 1900, "bash", ["bash"], mb=5)
        self.add(3100, 1950, "claude", ["claude"], cpu=2.4, mb=286, threads=18)
        self.add(3300, 1951, "python3", ["python3", "train.py", "--workers", "4"], cpu=368, mb=3420,
                 threads=9, cwd="/home/alex/ml", disk=58 * MB)
        venv = "/home/alex/llm/venv/bin"
        vllm = [f"{venv}/python3", f"{venv}/vllm", "serve", "meta-llama/Llama-3.1-8B-Instruct", "--port", "8000",
                "--served-model-name", "llama-3.1-8b"]
        self.add(3500, 1952, "vllm", vllm, cpu=82, mb=2180, threads=140, cwd="/home/alex/llm")
        self.add(3561, 3500, "VLLM::EngineCor", vllm, cpu=99, mb=4310, threads=88, cwd="/home/alex/llm")
        self.add(3700, 1953, "python3", ["python3", "serve.py"], cpu=0.8, mb=74, threads=3, cwd="/home/alex/blog")
        self.add(3710, 1953, "python3", ["python3", "-m", "tools.build", "--watch"], cpu=1.9, mb=61, cwd="/home/alex/blog")

    def add(self, pid, ppid, name, cmd, *, uid=ME, user="alex", threads=1, cpu=0.0, mb=0, cwd=None, disk=0.0):
        if len(name) >= 15 and cmd and os.path.basename(cmd[0]).startswith(name):
            name = os.path.basename(cmd[0])  # what Sampler does with a name /proc cut at 15 characters
        self.rows.append((pid, ppid, name, uid, user, cmd, threads, cpu, mb, cwd, disk))


class FakeGpus:
    handles = [None, None]
    names = ["NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 3090"]
    driver = "570.133.07"

    def close(self) -> None:
        pass


class FakeSampler(tm.Sampler):
    """What Sampler gives the app, made up: nothing is read from the machine."""

    def __init__(self) -> None:  # noqa: D107 - no super().__init__: that reads the machine
        self.ncpu, self.ncores = NCPU, NCPU // 2
        self.cpu_model = "AMD Ryzen 9 7950X 16-Core Processor"
        self.me, self.users = ME, {}
        self.desk = {"firefox": "Firefox", "code": "Visual Studio Code", "nautilus": "Files"}
        self.rule_cache = {}
        self.disks = [tm.DiskDev("nvme0n1", "Samsung SSD 990 PRO 2TB", 2_000_398_934_016, True, True),
                      tm.DiskDev("sda", "WDC WD40EFRX-68N32N0", 4_000_787_030_016, False, False, None)]
        self.made_up_disks = list(self.disks)
        self.nics = ["enp6s0"]
        self.gpus = FakeGpus()
        half = NCPU // 2
        self.cpu_groups = [(f"CCD {d}", [c for k in range(d * 8, d * 8 + 8) for c in (k, k + half)]) for d in (0, 1)]
        self.fake, self.k, self.rnd = Fake(), 0, random.Random(7)
        self.totals = {"nvme0n1": [0.0, 0.0], "enp6s0": [196 * GB, 24 * GB]}

    def wave(self, period: float, phase: float = 0.0) -> float:
        return math.sin(self.k / period + phase)

    def sample(self) -> tm.Snapshot:
        self.k += 1
        r, w = self.rnd, self.wave
        burst = max(0.0, w(9, 1.0)) ** 3                       # every so often the data job writes
        job = min(1.0, max(0.15, 0.6 + 0.45 * w(11) + 0.1 * r.uniform(-1, 1)))   # the data job, in waves
        serve = min(1.0, max(0.3, 0.75 + 0.25 * w(7, 1.3) + 0.08 * r.uniform(-1, 1)))  # vLLM, by requests
        load = {3300: job, 3500: serve, 3561: serve}
        train = min(100.0, 40 + 58 * job + 3 * r.uniform(-1, 1))   # the training job on the second card
        procs = {}
        for pid, ppid, name, uid, user, cmd, threads, cpu, mb, cwd, disk in self.fake.rows:
            kernel = pid == 2 or ppid == 2
            c = cpu * load[pid] if pid in load else cpu * (1 + 0.4 * r.uniform(-1, 1))
            gpu = {0: int(19.6 * GB)} if pid == 3561 else {0: 210 * MB} if pid == 1850 else {}
            gpu = {0: 180 * MB} if pid == 2400 else {1: int(17.8 * GB)} if pid == 3300 else gpu
            procs[pid] = tm.Proc(
                pid=pid, ppid=ppid, ct=float(pid), name=name, uid=uid, user=user, status="running" if c > 50 else "sleeping",
                threads=threads, cmdline=cmd, cpu=max(0.0, c) / NCPU, mem=None if kernel else int(mb * MB),
                disk=None if kernel or uid != ME else disk * (0.6 + burst + 0.3 * r.random()),
                gpu_mem=gpu, gpu_util=min(100.0, 95 * serve + 3 * r.uniform(-1, 1)) if pid == 3561
                else train if pid == 3300 else 1.0 if pid == 2400 else 0.0, cwd=cwd)
        times, freq = [], []
        hot = {cpu: pct * load.get(pid, 1.0) for pid, _, cpu, pct in HOT}
        for i in range(NCPU):
            busy = hot[i] * (0.97 + 0.03 * r.random()) if i in hot else r.uniform(0.5, 9) * (1.5 if i < 4 else 1)
            sys_part = busy * (0.08 if i in hot else 0.35)
            iow = r.uniform(1, 6) if i in (3, 4) and burst > 0.1 else 0.0
            times.append(NS(user=busy - sys_part, nice=0.0, system=sys_part * 0.7, irq=sys_part * 0.1,
                            softirq=sys_part * 0.2, steal=0.0, iowait=iow))
            freq.append(r.uniform(5350, 5720) if i in hot else r.uniform(2950, 4300))
        cpu = sum(min(100.0, t.user + t.system + t.irq + t.softirq) for t in times) / NCPU
        used = sum(p.mem or 0 for p in procs.values()) + 5.2 * GB + 0.4 * GB * w(40)
        total, cache = 64 * GB, 18.3 * GB
        vm = NS(total=total, used=used, available=total - used, percent=used / total * 100,
                free=total - used - cache, buffers=0.9 * GB, cached=cache - 0.9 * GB, shared=1.4 * GB)
        read = 58 * MB * (0.7 + 0.5 * r.random()) + 40 * MB * max(0.0, w(13, 2)) ** 4
        write = 2 * MB * r.random() + 310 * MB * burst
        self.totals["nvme0n1"][0] += read
        recv = 0.3 * MB + 9.5 * MB * max(0.0, w(17, 0.5)) ** 6 * (0.8 + 0.4 * r.random())
        sent = 0.1 * MB + 1.2 * MB * r.random() ** 3
        nvme, hdd = self.made_up_disks
        g = procs[3561].gpu_util
        disks = [
            tm.DiskInfo(nvme, read, write, min(100.0, (read + write) / (14 * MB)), 0.12 + 0.2 * r.random(),
                        ["/", "/boot/efi"], temp=42 + burst * 4 + r.random(),
                        space=[("/", int(702 * GB), int(1823 * GB), 38.5), ("/boot/efi", 6 * MB, 1074 * MB, 0.6)],
                        health={"percent_used": 3, "avail_spare": 100, "total_data_written": int(21.4 * 1024 * GB),
                                "total_data_read": int(35.2 * 1024 * GB), "power_on_hours": 4382,
                                "unsafe_shutdowns": 19, "media_errors": 0, "warnings": []}),
            tm.DiskInfo(hdd, 0.0, 0.2 * MB * r.random(), r.uniform(0, 2), 4.1 + r.random(), ["/mnt/backup"],
                        space=[("/mnt/backup", int(2.61 * 1024 * GB), int(3.64 * 1024 * GB), 71.7)])]
        nics = [tm.NicInfo("enp6s0", False, True, 2500, ["192.168.1.42"], ["fe80::2e0:4cff:fe68:1f2a"], recv, sent,
                           int(self.totals["enp6s0"][0] + recv * self.k), int(self.totals["enp6s0"][1] + sent * self.k))]
        on = [{pid: p.gpu_mem[i] for pid, p in procs.items() if i in p.gpu_mem} for i in (0, 1)]
        gpus = [tm.GpuInfo(0, FakeGpus.names[0], g, sum(on[0].values()) + 310 * MB, 24 * GB, 64 + 3 * w(30),
                           290 + 40 * w(4) + 10 * r.random(), 450.0, 47 + 3 * w(30), int(2610 + 60 * w(6)), 10501,
                           on[0]),
                tm.GpuInfo(1, FakeGpus.names[1], train, sum(on[1].values()) + 260 * MB, 24 * GB, 71 + 2 * w(25, 0.7),
                           300 + 30 * w(5, 2.0) + 8 * r.random(), 350.0, 62 + 4 * w(25, 0.7),
                           int(1830 + 45 * w(7, 1.0)), 9751, on[1])]
        user_procs = [p for p in procs.values() if not p.kernel]
        return tm.Snapshot(
            t=1773494525.0 + self.k, cpu=cpu, freq=sum(freq) / NCPU, freq_max=5881.0,
            load=(3.4 + 0.3 * w(20), 3.1, 2.9), uptime=273_153 + self.k, vm=vm, swap=NS(total=8 * GB, used=0),
            gpus=gpus, disks=disks, nics=nics, procs=procs, groups=self.group(procs), nprocs=len(user_procs),
            nthreads=sum(p.threads for p in user_procs), per_cpu_times=times, per_cpu_freq=freq,
            cpu_temp=52 + 12 * job, core_top={cpu: (hot[cpu], pid, name) for pid, name, cpu, _ in HOT})


class ShotApp(tm.TaskManager):
    """Reads only when told to, so every run feeds in the same readings."""

    async def tick(self, force: bool = False, fed: bool = False) -> None:
        if fed:
            await super().tick(force=True)


def rgb(color, default):
    if color is None or color.is_default:
        return default
    t = color.get_truecolor()
    return (t.red, t.green, t.blue)


def blend(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Painter:
    def __init__(self) -> None:
        size = 14 * SCALE
        self.font = ImageFont.truetype(str(FONTS / "DejaVuSansMono.ttf"), size)
        self.bold = ImageFont.truetype(str(FONTS / "DejaVuSansMono-Bold.ttf"), size)
        self.cw = round(self.font.getlength("M"))
        self.ch = sum(self.font.getmetrics())  # box-drawing strokes span exactly this, so rows join up
        self.pad = 16 * SCALE

    def paint(self, lines, path: Path) -> None:
        w, h = COLS * self.cw + 2 * self.pad, ROWS * self.ch + 2 * self.pad
        img = Image.new("RGBA", (w, h), BG + (255,))
        d = ImageDraw.Draw(img)
        for y, line in enumerate(lines[:ROWS]):
            x = 0
            for seg in line:
                if seg.control:
                    continue
                st = seg.style or Style()
                fg, bg = rgb(st.color, FG), rgb(st.bgcolor, BG)
                if st.reverse:
                    fg, bg = bg, fg
                if st.dim:
                    fg = blend(fg, bg, 0.45)
                for c in seg.text:
                    n = cell_len(c)
                    X, Y = self.pad + x * self.cw, self.pad + y * self.ch
                    d.rectangle([X, Y, X + n * self.cw - 1, Y + self.ch - 1], fill=bg)
                    self.glyph(d, c, X, Y, fg, bool(st.bold))
                    x += n
        mask = Image.new("L", (w, h), 0)  # rounded corners, like a window
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=10 * SCALE, fill=255)
        img.putalpha(mask)
        img.save(path, optimize=True)

    def glyph(self, d, c, X, Y, fg, bold) -> None:
        cw, ch = self.cw, self.ch
        if c == " ":
            return
        if c == "█":
            d.rectangle([X, Y, X + cw - 1, Y + ch - 1], fill=fg)
        elif c in "▁▂▃▄▅▆▇":
            d.rectangle([X, Y + ch - ch * ("▁▂▃▄▅▆▇".index(c) + 1) / 8, X + cw - 1, Y + ch - 1], fill=fg)
        elif c in "▏▎▍▌▋▊▉":
            d.rectangle([X, Y, X + cw * ("▏▎▍▌▋▊▉".index(c) + 1) / 8 - 1, Y + ch - 1], fill=fg)
        elif c == "▔":
            d.rectangle([X, Y, X + cw - 1, Y + ch / 8 - 1], fill=fg)
        elif c == "▕":
            d.rectangle([X + cw * 7 / 8, Y, X + cw - 1, Y + ch - 1], fill=fg)
        else:
            d.text((X, Y), c, font=self.bold if bold else self.font, fill=fg, anchor="la")


async def shoot(names: list[str]) -> None:
    app, painter = ShotApp(FakeSampler(), 1.0), Painter()

    def screen_lines():
        update = app.screen._compositor.render_update(full=True, screen_stack=app._background_screens, simplify=True)
        return [[seg for strip in row for seg in strip] for row in update.strips]

    async def snap(name: str) -> None:
        app.render_now()
        await pilot.pause(0.2)
        if name in names:
            painter.paint(screen_lines(), HERE / f"{name}.png")
            print(f"wrote docs/{name}.png")

    async with app.run_test(size=(COLS, ROWS)) as pilot:
        for _ in range(STEPS):
            await app.tick(fed=True)
        app.expanded.add("app:apps:Firefox")
        app.render_now()
        await pilot.pause(0.2)
        table = app.query_one("#proc-table")
        table.move_cursor(row=next(i for i, k in enumerate(table.rows) if k.value == "g:vllm:8000"))
        await snap("processes")
        await pilot.press("2")
        await snap("performance")
        await pilot.press("l")
        await snap("cores")
        await pilot.press("l", "down", "down")
        await snap("disk")
        await pilot.press("4")
        await snap("gpu")


def main() -> None:
    names = sys.argv[1:] or ["processes", "performance", "cores", "disk", "gpu"]
    asyncio.run(shoot(names))


if __name__ == "__main__":
    main()
