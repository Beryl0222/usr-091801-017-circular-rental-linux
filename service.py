"""循环租用资产履约后端：健康探针与领域 API 入口。"""

import argparse

from rental.api import make_handler
from rental.engine import Engine

SERVICE_ID = "circular-rental"
SERVICE_NAME = "循环租用资产履约"


def health_payload():
    """返回服务身份和状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


_engine = Engine()
Handler = make_handler(_engine, health_payload)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["name"] == SERVICE_NAME
        probe = Engine()
        probe.execute(
            "register_asset",
            {
                "serial_no": "SN-CHECK",
                "model": "probe",
                "category": "camera",
                "owner_id": "self",
                "at": "2026-01-01T00:00:00Z",
            },
        )
        assert probe.query("verify_replay")["converged"]
        print("基础检查通过")
        return
    from http.server import ThreadingHTTPServer

    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
