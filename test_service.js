"use strict";

const { spawnSync } = require("node:child_process");

const modules = ["service_contract", "test_domain", "test_http_api"];

let failed = 0;
for (const mod of modules) {
  const result = spawnSync(
    "python3",
    ["-m", "unittest", "-v", mod],
    { stdio: "inherit" },
  );
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) failed += 1;
}

// 资产主管验收：乱序/重复投递收敛 + 争议阻断
const check = spawnSync("python3", ["service.py", "--check"], { stdio: "inherit" });
if (check.status !== 0) failed += 1;

process.exit(failed === 0 ? 0 : 1);
