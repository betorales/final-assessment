from flask import Flask, render_template

app = Flask(__name__)

@app.route("/")
def home():
    return render_template('home.html')

@app.route("/profile")
def profile():
    return render_template('profile.html')

@app.route('/screenings')
def screenings():
    return render_template('screenings.html')

@app.route('/libraries')
def libraries():
    return render_template('libraries.html')

@app.route('/filter-settings')
def filter_settings():
    return render_template('filter-settings.html')

@app.route('/alerts')
def alerts():
    render_template('alerts.html')

if __name__ == "__main__":
    app.run(debug=True)