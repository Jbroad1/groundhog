# groundhog demo — render a GIF for the README with charmbracelet/vhs:
#   vhs demo.tape        →  demo.gif
# https://github.com/charmbracelet/vhs
Output demo.gif

Set FontSize 16
Set Width 1200
Set Height 760
Set Padding 20
Set Theme "Dracula"

Type "python scripts/groundhog.py scan --config-dir assets/fixtures/sample-config --since all"
Sleep 500ms
Enter
Sleep 4s
