#!/usr/bin/env python3
"""Persist the LogBench listening port; standard-library-only, Python 3.9+."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


def validate_port(value):
    if type(value) is int:
        port = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        port = int(value)
    else:
        raise ValueError('端口必须为 1–65535 的十进制整数。')
    if not 1 <= port <= 65535:
        raise ValueError('端口范围必须为 1–65535。')
    return port


def set_port(port, root):
    """Return (old_port, new_port) after atomically updating only `port`."""
    port = validate_port(port)
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('找不到 LogBench 部署目录。')
    target = root / 'config.json'
    template = root / 'config.example.json'
    if target.is_symlink():
        raise ValueError('拒绝修改指向其他位置的 config.json 符号链接。')
    source = target if target.exists() else template
    if source.is_symlink() or not source.is_file():
        raise ValueError('缺少有效的 config.json 或 config.example.json。')
    try:
        config = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('配置文件不可读或 JSON 格式不正确；原文件未修改。') from exc
    if not isinstance(config, dict):
        raise ValueError('配置必须是 JSON 对象；原文件未修改。')
    previous = config.get('port', 50004)
    config['port'] = port
    # Only write when necessary, including explicit config creation from template.
    if target.is_file() and previous == port:
        return previous, port
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(root),
                                         prefix='.config-port-', suffix='.tmp',
                                         delete=False) as handle:
            temp_name = handle.name
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            os.chmod(temp_name, stat.S_IMODE(target.stat().st_mode))
        elif os.name != 'nt':
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return previous, port


def main(argv=None):
    parser = argparse.ArgumentParser(description='修改 LogBench 持久监听端口（无需额外依赖）')
    parser.add_argument('port', nargs='?', help='新端口；省略则交互输入')
    parser.add_argument('--home', type=Path, default=Path(__file__).resolve().parents[1],
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        value = args.port
        if value is None:
            value = input('请输入新的 LogBench 端口 [1-65535]：').strip()
        old, new = set_port(value, args.home)
    except (ValueError, OSError, EOFError, KeyboardInterrupt) as exc:
        print('端口修改失败：%s' % exc, file=sys.stderr)
        return 2
    print('端口设置已保存：%s → %s' % (old, new))
    print('配置文件：%s' % (Path(args.home).resolve() / 'config.json'))
    print('请重启 LogBench 服务后生效；如果使用 Nginx/Docker，须同步更改代理或端口映射。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
