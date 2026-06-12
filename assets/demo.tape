# Tako VM README demo — records real requests against a live local server
Output assets/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 940
Set Height 540
Set Padding 16
Set TypingSpeed 20ms
Set Theme "Catppuccin Mocha"

Hide
Type `export PS1='$ ' && clear`
Enter
Show

# Scene 1: simple sync execution
Type `# Run Python in an isolated container`
Enter
Type `curl -s localhost:8000/execute -X POST -H 'Content-Type: application/json' -d '{"code": "print(1 + 1)"}' | jq`
Enter
Sleep 4.5s

Type `clear`
Enter

# Scene 2: runtime dependencies
Type `# Need packages? Installed at runtime, cached for repeats`
Enter
Type `curl -s localhost:8000/execute -X POST -H 'Content-Type: application/json' -d '{"code": "import numpy as np\nprint(np.arange(6).reshape(2, 3))", "requirements": ["numpy"]}' | jq -r .stdout`
Enter
Sleep 6s

# Scene 3: network isolation
Type `# Network? Blocked by default`
Enter
Type `curl -s localhost:8000/execute -X POST -H 'Content-Type: application/json' -d '{"code": "import urllib.request\nurllib.request.urlopen(\"https://example.com\")", "timeout": 15}' | jq -r '.stderr' | tail -2`
Enter
Sleep 6s
