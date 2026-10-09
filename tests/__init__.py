"""CrossPC 测试包。

存在的唯一理由: `python -m unittest discover -s tests -t <项目根>` 要求
start 目录是一个"可导入的"目录(unittest.loader 会 import 它)。没有这个
文件时 Python 只把它当 namespace package, 某些 Python 版本/布局下
discover 会直接报 "Start directory is not importable"。

测试本身不依赖任何第三方库(不用 pytest), 所以这个包是空的。
"""
