#!/usr/bin/env python3
"""Landlock launcher: restrict THIS process, then exec the command.

    python3 -I -S landlock_exec.py --policy '<json>' -- bash -c '...'

Standalone stdlib script (no server imports) so it starts in ~20 ms and the
restriction applies only to the exec'd child tree — the Bob server itself
stays unconfined. Landlock is unprivileged (no namespaces, so AppArmor's
userns restriction doesn't apply) and irrevocable for every descendant.

Policy JSON:
    {"rw": [paths], "ro": [paths], "tcp_connect": [ports] | null,
     "scope": true}
- rw/ro: path-beneath grants; missing paths are skipped (bind-try).
  Everything not granted is denied (EACCES).
- tcp_connect: ports a TCP connect() may target (Landlock ABI >= 4);
  null = TCP unrestricted. DNS (UDP) is never restricted by Landlock.
- scope: confine signals + abstract unix sockets to the sandbox (ABI >= 6)
  — bash can't kill/signal bob.service.
- seccomp: refuse socket(AF_UNIX) with EACCES and io_uring with ENOSYS.
  Landlock has no right for connecting to PATHNAME unix sockets on this
  kernel, so without this, systemd-run --user (a full escape), docker
  (the docker group is root-equivalent), tailscale and ssh-agent are all
  reachable (2026-10-12 audit). socketpair stays allowed (multiprocessing,
  pipes). io_uring is refused because it bypasses seccomp. Needs
  libseccomp.so.2; fails closed (exit 125) if requested and missing.

Exit 125 with a "landlock:" message on stderr if Landlock is unavailable
or the policy can't be applied — the caller decides whether to fall back.
``--probe`` prints the ABI version and exits 0 (availability check).
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import struct
import sys

_NR_CREATE, _NR_ADD_RULE, _NR_RESTRICT = 444, 445, 446  # x86_64 + aarch64
_PR_SET_NO_NEW_PRIVS = 38

# Filesystem access rights by ABI version.
_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_REMOVE_DIR, _REMOVE_FILE, _MAKE_CHAR, _MAKE_DIR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
_MAKE_REG, _MAKE_SOCK, _MAKE_FIFO, _MAKE_BLOCK = 1 << 8, 1 << 9, 1 << 10, 1 << 11
_MAKE_SYM, _REFER, _TRUNCATE, _IOCTL_DEV = 1 << 12, 1 << 13, 1 << 14, 1 << 15

_FS_V1 = (1 << 13) - 1
_FILE_ONLY = _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE | _IOCTL_DEV
_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX, _SCOPE_SIGNAL = 1 << 0, 1 << 1

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def _buf(fmt: str, *vals: int) -> ctypes.Array:
    """Kernel struct as a raw buffer (native-endian, no padding) —
    landlock_path_beneath_attr is __attribute__((packed))."""
    return ctypes.create_string_buffer(struct.pack(fmt, *vals))


def _die(msg: str) -> None:
    sys.stderr.write(f"landlock: {msg}\n")
    sys.exit(125)


def abi_version() -> int:
    v = _libc.syscall(_NR_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    return int(v) if v > 0 else 0


def _fs_mask(abi: int) -> int:
    mask = _FS_V1
    if abi >= 2:
        mask |= _REFER
    if abi >= 3:
        mask |= _TRUNCATE
    if abi >= 5:
        mask |= _IOCTL_DEV
    return mask


def apply(policy: dict) -> int:
    abi = abi_version()
    if abi < 1:
        _die("not supported by this kernel (or blocked by seccomp)")
    fs = _fs_mask(abi)
    ro = _EXECUTE | _READ_FILE | _READ_DIR
    ports = policy.get("tcp_connect")
    net = _NET_CONNECT_TCP if (ports is not None and abi >= 4) else 0
    scoped = (_SCOPE_ABSTRACT_UNIX | _SCOPE_SIGNAL
              if (policy.get("scope") and abi >= 6) else 0)
    size = 24 if scoped else (16 if net else 8)
    attr = _buf("=QQQ", fs, net, scoped)
    fd = _libc.syscall(_NR_CREATE, attr, ctypes.c_size_t(size),
                       ctypes.c_uint32(0))
    if fd < 0:
        _die(f"create_ruleset failed (errno {ctypes.get_errno()})")

    def grant(path: str, access: int) -> None:
        try:
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            return  # missing path — skip, like bwrap's --bind-try
        try:
            if not os.path.isdir(path):
                access &= _FILE_ONLY
            rule = _buf("=Qi", access & fs, pfd)
            if _libc.syscall(_NR_ADD_RULE, ctypes.c_int(fd), ctypes.c_int(1),
                             rule, ctypes.c_uint32(0)) < 0:
                _die(f"add_rule {path} failed (errno {ctypes.get_errno()})")
        finally:
            os.close(pfd)

    for p in policy.get("ro") or []:
        grant(p, ro)
    for p in policy.get("rw") or []:
        grant(p, fs)
    if net:
        for port in ports or []:
            rule = _buf("=QQ", _NET_CONNECT_TCP, int(port))
            if _libc.syscall(_NR_ADD_RULE, ctypes.c_int(fd), ctypes.c_int(2),
                             rule, ctypes.c_uint32(0)) < 0:
                _die(f"add_rule port {port} failed (errno {ctypes.get_errno()})")

    if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _die("prctl(NO_NEW_PRIVS) failed")
    if _libc.syscall(_NR_RESTRICT, ctypes.c_int(fd), ctypes.c_uint32(0)) < 0:
        _die(f"restrict_self failed (errno {ctypes.get_errno()})")
    os.close(fd)
    return abi


_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_SCMP_CMP_EQ = 4
_AF_UNIX = 1


def apply_seccomp() -> None:
    try:
        lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError:
        _die("seccomp requested but libseccomp.so.2 is missing")
        return
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    ctx = ctypes.c_void_p(lib.seccomp_init(ctypes.c_uint32(_SCMP_ACT_ALLOW)))
    if not ctx:
        _die("seccomp_init failed")

    def nr(name: str) -> int:
        n = lib.seccomp_syscall_resolve_name(name.encode())
        if n < 0:
            _die(f"seccomp: unknown syscall {name}")
        return n

    # struct scmp_arg_cmp { uint32 arg; int32 op; uint64 a; uint64 b; }
    unix_only = _buf("=IiQQ", 0, _SCMP_CMP_EQ, _AF_UNIX, 0)
    if lib.seccomp_rule_add_array(ctx, ctypes.c_uint32(_SCMP_ACT_ERRNO | 13),
                                  nr("socket"), 1, unix_only) != 0:
        _die("seccomp: socket rule failed")
    for name in ("io_uring_setup", "io_uring_enter", "io_uring_register"):
        if lib.seccomp_rule_add_array(ctx, ctypes.c_uint32(_SCMP_ACT_ERRNO | 38),
                                      nr(name), 0, None) != 0:
            _die(f"seccomp: {name} rule failed")
    if lib.seccomp_load(ctx) != 0:
        _die("seccomp_load failed")
    lib.seccomp_release(ctx)


def main(argv: list[str]) -> None:
    if argv[1:2] == ["--probe"]:
        print(abi_version())
        sys.exit(0)
    if len(argv) < 5 or argv[1] != "--policy" or argv[3] != "--":
        _die("usage: landlock_exec.py --policy JSON -- CMD [ARGS...]")
    try:
        policy = json.loads(argv[2])
    except ValueError as e:
        _die(f"bad policy JSON: {e}")
        return
    # Resolve the executable BEFORE restricting: after restrict_self a PATH
    # walk can hit unreadable dirs, and nothing may be lazily imported from
    # a stdlib the policy doesn't grant (2026-10-12: a Linuxbrew python
    # died importing warnings.py post-restriction).
    exe = shutil.which(argv[4]) or argv[4]
    apply(policy)
    if policy.get("seccomp"):
        apply_seccomp()   # after NO_NEW_PRIVS (set in apply)
    try:
        os.execv(exe, argv[4:])
    except OSError as e:
        sys.stderr.write(f"landlock: exec {exe} failed: {e}\n")
        sys.exit(127)


if __name__ == "__main__":
    main(sys.argv)
