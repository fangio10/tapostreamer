import os
os.environ["LD_LIBRARY_PATH"] = "/lib/x86_64-linux-gnu:" + os.environ.get("LD_LIBRARY_PATH", "")
os.environ["PATH"] = "/usr/bin:" + os.environ.get("PATH", "")
os.environ["DISPLAY"] = os.environ.get("DISPLAY", ":0")
