from flask import Flask
from threading import Thread
import os

app = Flask(__name__)

@app.route("/")
def home():
    return "AURACITY BOT is running!"

def _run():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

def server_on():
    t = Thread(target=_run)
    t.daemon = True
    t.start()
