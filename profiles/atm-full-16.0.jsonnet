// atm-full-16.0: FDE without TPM2 auto-unlock, for SLES 16.0.
//
// Same as atm-full.jsonnet but omits encryption.luks2.tpm, which is not
// available before Agama 16.1 (see atm-full.jsonnet for the reference link).
// The disk is still LUKS2-encrypted; the passphrase must be entered by hand
// at every boot.
{
  product: {
    id: "SLES",
  },
  hostname: {
    "static": "sles16-atm-full",
  },
  root: {
    password: "DemoSecurity2026!",
  },
  storage: {
    drives: [
      {
        partitions: [
          { filesystem: { path: "/boot", type: "ext4" }, size: "1 GiB" },
          {
            filesystem: { path: "/", type: "btrfs" },
            encryption: {
              luks2: { password: "DemoSecurity2026!" },
            },
          },
        ],
      },
    ],
  },
}
