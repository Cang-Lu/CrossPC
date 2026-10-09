"""CrossPC test package.

It exists for one reason only: `python -m unittest discover -s tests -t <project
root>` requires the start directory to be an "importable" directory
(unittest.loader will import it). Without this file Python treats it merely as a
namespace package, and on some Python versions/layouts discover fails outright
with "Start directory is not importable".

The tests themselves depend on no third-party library (no pytest), so this
package is empty.
"""
