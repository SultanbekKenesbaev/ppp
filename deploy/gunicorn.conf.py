bind = "unix:/run/taskplatform/taskplatform.sock"
workers = 2
worker_class = "gevent"
worker_connections = 1000
timeout = 60
keepalive = 5
accesslog = "-"
errorlog = "-"
