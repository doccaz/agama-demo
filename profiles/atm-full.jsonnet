// atm-full: FDE + TPM2 auto-unlock. SLES 16.1+ ONLY.
//
// storage.drives[0].partitions[1].encryption.luks2.tpm is documented as
// available "Since version 16.1" in the Agama storage profile reference:
// https://agama-project.github.io/docs/user/reference/profile/storage
//
// On SLES 16.0 use atm-full-16.0.jsonnet instead: the disk is still
// LUKS2-encrypted, but without this field the passphrase has to be typed
// by hand at every boot instead of being unsealed from the TPM.
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
              luks2: { password: "DemoSecurity2026!", tpm: true },
            },
          },
        ],
      },
    ],
  },
}
