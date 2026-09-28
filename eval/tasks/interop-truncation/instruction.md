Use the envelope `run` tool to execute exactly this command:

```
seq 1 20000 | awk '{print "row " $1} NR==<<line>> {print "TOKEN=" <<factors>>}'
```

The output is long and will be shortened. One line starts with TOKEN= . Find it without re-running the command and reply with only the value after TOKEN=.
