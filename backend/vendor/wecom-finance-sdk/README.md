# Enterprise WeChat Finance SDK mount

Place the official Linux `libWeWorkFinanceSdk_C.so` for the Docker worker's
CPU architecture in this directory. The binary is proprietary and is not
committed to the repository. Docker Compose mounts this directory read-only at
`/opt/wecom-finance-sdk` in the worker container.

Use the Linux x86_64 SDK for an amd64 worker image and the Linux arm64 SDK for
an arm64 worker image. A mismatched binary fails at startup with a clear
configuration error when the archive poller first runs.
