import os
if os.environ.get("COV_CMP") == "1":
    import covplugin5  # noqa: F401  (installs the comparison-profiling import hook)
