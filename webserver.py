from flask import Flask
from threading import Thread

app = Flask('')
@app.route('/')
def index():
    return "discord bot okay"

def run():
    app.run(host='0.0.0.0', port=8080, debug=True)

def keep_alive():
    t = Thread(target=run)
    t.start()