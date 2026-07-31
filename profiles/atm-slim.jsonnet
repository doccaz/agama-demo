// atm-slim: Btrfs root, no encryption. Works unchanged on SLES 16.0 and 16.1.
{
  product: {
    id: "SLES",
  },
  hostname: {
    "static": "sles16-atm-slim",
  },
  root: {
    password: "DemoSecurity2026!",
  },
  storage: {
    drives: [
      {
        partitions: [
          { filesystem: { path: "/", type: "btrfs" } },
        ],
      },
    ],
  },
}
