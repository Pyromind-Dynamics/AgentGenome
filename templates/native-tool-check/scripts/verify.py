from pathlib import Path
import sys

assert Path(sys.argv[1]).read_text() == "agentgenome real tool\n"
