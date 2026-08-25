# The README's demo gif: `vhs docs/demo.tape`.
#
# IB_DB points at a throwaway copy on purpose. A demo that drills and rates
# writes reviews, and the bank in the recording is a real one:
#
#   sqlite3 ib.db ".backup /tmp/superday-demo.db"
#
# SUPERDAY_NO_MOUSE keeps mode 1003 off. vhs feeds keystrokes, so a mouse
# tracking stream in the recording is bytes nothing will ever read.
#
# The key counts below are exact. `drill -n 2` wants Enter to reveal and a
# digit to rate, twice; miscount them and the next command gets typed into an
# answer prompt instead of the shell.

Output docs/demo.gif

Set Shell "bash"
# 24 rather than the default 50: nothing here moves faster than a keystroke,
# and half the frames is half the megabytes in a README that has to load.
Set Framerate 24
Set FontSize 15
Set Width 1360
Set Height 700
Set Padding 14
Set BorderRadius 8
Set Theme { "background": "#17151b", "foreground": "#d8d4dc", "cursor": "#ff5f9e", "selection": "#2d2935" }

Hide
Type "cd ~/superday" Enter
Type "export IB_DB=/tmp/superday-demo.db" Enter
Type "export SUPERDAY_NO_MOUSE=1" Enter
Type "clear" Enter
Show

Type "./superday" Sleep 400ms Enter
Sleep 2500ms

# A drill, self-rated. Nothing on this path makes a network call.
Type "drill -t dcf -n 2 --local" Sleep 400ms Enter
Sleep 2500ms
Enter Sleep 4s
Type "3" Sleep 400ms Enter
Sleep 2s
Enter Sleep 4s
Type "4" Sleep 400ms Enter
Sleep 3500ms

# The browser: four arrows and Enter, no modifier chords anywhere.
#
# Deliberately into Technicals and not Behavioural. The `firms` family under
# Behavioural is a list of the banks one person is applying to, and this
# recording goes in a public README -- same rule as `sec_contact` shipping
# empty.
Type "browse" Sleep 400ms Enter
Sleep 2s
Right Sleep 1400ms
Down Sleep 600ms
Down Sleep 600ms
Down Sleep 800ms
Right Sleep 1600ms
Down Sleep 800ms
Down Sleep 1400ms
Escape Sleep 1s

# Readiness, one tab at a time.
Type "dashboard" Sleep 400ms Enter
Sleep 3s
Right Sleep 2s
Right Sleep 2500ms
Escape Sleep 800ms

Type "exit" Sleep 300ms Enter
Sleep 1500ms
