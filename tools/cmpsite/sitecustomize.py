import os
if os.environ.get("COV_CMP") == "1" or os.environ.get("COV_BOOL") == "1":
    import covplugin5  # noqa: F401  (installs the profiling import hook)
