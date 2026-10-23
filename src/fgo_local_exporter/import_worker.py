"""原文件只经匿名管道进入有界子进程，stdout 只输出白名单结果。"""

import json
import sys

from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.statistics import decode_response, export_statistics

__all__ = ["main"]


def main():
    try:
        if sys.platform == "linux":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (8, 9))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        if not raw or len(raw) > 16 * 1024 * 1024:
            raise ExportError("INPUT_TOO_LARGE")
        body, preview = export_statistics(decode_response(raw), allow_jp=True)
        raw = None
        header = json.dumps(preview, ensure_ascii=False).encode()
        if len(header) > 32768 or len(body) > 32 * 1024 * 1024:
            raise ExportError("STATISTICS_LIMIT")
        output = b"O" + len(header).to_bytes(4, "big") + header + body
    except BaseException as error:
        code = error.code if isinstance(error, ExportError) else "IMPORT_INVALID"
        output = b"E" + code.encode("ascii")
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
