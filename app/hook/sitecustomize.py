import os

if os.environ.get("BR_OVERLAY"):
    try:
        import br_overlay

        br_overlay.install()
    except Exception:
        pass
