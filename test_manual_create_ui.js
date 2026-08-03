const test = require("node:test");
const assert = require("node:assert/strict");
const {
  normalizeCatalog,
  compatibleServerTypes,
  buildPayload,
} = require("./static/manual-create.js");

const rawCatalog = {
  name: "2",
  default_source: "snapshot",
  default_snapshot_id: 412977893,
  default_server_type: "cx33",
  default_location: "nbg1",
  snapshots: [
    { id: 412977893, label: "main", architecture: "x86", disk_size: 80 },
    { id: 412977894, label: "large", architecture: "x86", disk_size: 120 },
  ],
  system_images: [
    { id: 1001, name: "debian-12", label: "Debian 12", architecture: "x86", disk_size: 5 },
  ],
  server_types: [
    { name: "cx23", architecture: "x86", cores: 2, memory: 4, disk: 40 },
    { name: "cx33", architecture: "x86", cores: 4, memory: 8, disk: 80 },
    { name: "cx43", architecture: "x86", cores: 8, memory: 16, disk: 160 },
  ],
  locations: ["nbg1", "fsn1", "hel1"],
  ssh_keys: [{ id: 77, name: "shoo", fingerprint: "SHA256:test" }],
};

test("catalog normalization preserves dynamic IDs and defaults", () => {
  const catalog = normalizeCatalog(rawCatalog);
  assert.equal(catalog.name, "2");
  assert.equal(catalog.defaultSource, "snapshot");
  assert.equal(catalog.defaultImageId, 412977893);
  assert.equal(catalog.defaultServerType, "cx33");
  assert.equal(catalog.defaultLocation, "nbg1");
  assert.deepEqual(catalog.sshKeyIds, [77]);
});

test("system mode can select Debian and CX23", () => {
  const catalog = normalizeCatalog(rawCatalog);
  const types = compatibleServerTypes(catalog, "system", 1001);
  assert.deepEqual(types.map((row) => row.name), ["cx23", "cx33", "cx43"]);
});

test("large snapshot excludes server types with smaller disks", () => {
  const catalog = normalizeCatalog(rawCatalog);
  const types = compatibleServerTypes(catalog, "snapshot", 412977894);
  assert.deepEqual(types.map((row) => row.name), ["cx43"]);
});

test("official image payload includes selected project SSH keys", () => {
  assert.deepEqual(
    buildPayload({
      name: "2",
      source: "system",
      imageId: 1001,
      serverType: "cx23",
      location: "nbg1",
      allowFallback: true,
      sshKeyIds: [77],
    }),
    {
      name: "2",
      source: "system",
      image_id: 1001,
      server_type: "cx23",
      preferred_location: "nbg1",
      allow_fallback: true,
      ssh_key_ids: [77],
    },
  );
});

test("snapshot payload never includes SSH keys", () => {
  const payload = buildPayload({
    name: "2",
    source: "snapshot",
    imageId: 412977893,
    serverType: "cx33",
    location: "fsn1",
    allowFallback: false,
    sshKeyIds: [77],
  });
  assert.deepEqual(payload, {
    name: "2",
    source: "snapshot",
    image_id: 412977893,
    server_type: "cx33",
    preferred_location: "fsn1",
    allow_fallback: false,
  });
});
