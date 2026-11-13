"""In-Pod GPU acceptance, run before any model download.

Stdlib-only and import-free: ``ssh.SSH_BOOTSTRAP`` embeds this exact source.
``rows`` are ``nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap
--format=csv,noheader,nounits`` lines; ``rules`` is ``hardware.identity()``.
"""


def gpu_mismatch(rows, rules):  # noqa: PLR0911 -- one reason per failed rule
    """Return why the observed GPUs fail the bound rules, or None when accepted.

    An exact verified NVML name passes the identity check. Otherwise an allowed
    compute capability is the generic fallback, so a new display string never
    needs a code change. VRAM, driver and GPU count minimums always apply.
    """
    if len(rows) != rules["gpu_count"]:
        return f"expected {rules['gpu_count']} GPU(s), observed {len(rows)}"
    for row in rows:
        fields = [part.strip() for part in row.split(",")]
        if len(fields) != 4:  # noqa: PLR2004 -- the four queried columns
            return "unreadable nvidia-smi row"
        name, driver, vram, capability = fields
        try:
            driver_major = int(driver.split(".")[0])
            vram_mib = int(float(vram))
        except ValueError:
            return f"unreadable driver or memory for {name}"
        if driver_major < rules["min_driver_major"]:
            return f"driver {driver} is older than major {rules['min_driver_major']}"
        if vram_mib < rules["min_vram_mib"]:
            return f"{name} has {vram_mib} MiB, below {rules['min_vram_mib']} MiB"
        if name in rules["nvml_names"]:
            continue
        if capability in rules["compute_capabilities"]:
            continue
        return (
            f"{name} (compute {capability}) is neither a verified name for the "
            "bound GPU nor an allowed compute capability"
        )
    return None
