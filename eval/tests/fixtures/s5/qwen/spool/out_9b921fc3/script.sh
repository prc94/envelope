seq 1 20000 | awk '{print "row " $1} NR==12345 {print "TOKEN=" 7*11*13*17}'}
