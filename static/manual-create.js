(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ManualCreate = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function asId(value) {
    const id = Number(value);
    return Number.isInteger(id) ? id : null;
  }

  function normalizeCatalog(raw) {
    const data = raw || {};
    const snapshots = Array.isArray(data.snapshots) ? data.snapshots : [];
    const systemImages = Array.isArray(data.system_images) ? data.system_images : [];
    const serverTypes = Array.isArray(data.server_types) ? data.server_types : [];
    const sshKeys = Array.isArray(data.ssh_keys) ? data.ssh_keys : [];
    const defaultSource = data.default_source === "system" ? "system" : "snapshot";
    const sourceImages = defaultSource === "system" ? systemImages : snapshots;
    const requestedDefault = asId(data.default_snapshot_id);
    const defaultImage = sourceImages.find((row) => asId(row.id) === requestedDefault) || sourceImages[0];
    return {
      name: String(data.name || ""),
      defaultSource,
      defaultImageId: defaultImage ? asId(defaultImage.id) : null,
      defaultServerType: String(data.default_server_type || serverTypes[0]?.name || ""),
      defaultLocation: String(data.default_location || data.locations?.[0] || ""),
      snapshots,
      systemImages,
      serverTypes,
      locations: Array.isArray(data.locations) ? data.locations : [],
      sshKeys,
      sshKeyIds: sshKeys.map((row) => asId(row.id)).filter((id) => id !== null),
    };
  }

  function compatibleServerTypes(catalog, source, imageId) {
    const images = source === "system" ? catalog.systemImages : catalog.snapshots;
    const selectedId = asId(imageId);
    const image = images.find((row) => asId(row.id) === selectedId);
    if (!image) return [];
    const imageDisk = Number(image.disk_size || 0);
    const architecture = String(image.architecture || "").toLowerCase();
    return catalog.serverTypes.filter((row) => {
      const typeArchitecture = String(row.architecture || "").toLowerCase();
      const disk = Number(row.disk || 0);
      return (!architecture || architecture === typeArchitecture) && (!imageDisk || !disk || disk >= imageDisk);
    });
  }

  function buildPayload(dialog) {
    const payload = {
      name: String(dialog.name || ""),
      source: dialog.source === "system" ? "system" : "snapshot",
      image_id: asId(dialog.imageId),
      server_type: String(dialog.serverType || ""),
      preferred_location: String(dialog.location || ""),
      allow_fallback: Boolean(dialog.allowFallback),
    };
    if (payload.source === "system") {
      payload.ssh_key_ids = (dialog.sshKeyIds || [])
        .map(asId)
        .filter((id) => id !== null);
    }
    return payload;
  }

  return { normalizeCatalog, compatibleServerTypes, buildPayload };
});
