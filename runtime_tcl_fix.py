import os
import sys
base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
internal_dir = os.path.join(base_dir, '_internal')
os.environ['TCL_LIBRARY'] = os.path.join(internal_dir, 'tcl_assets', 'tcl8.6')
os.environ['TK_LIBRARY'] = os.path.join(internal_dir, 'tcl_assets', 'tk8.6')
