from club_app import create_app

app = create_app()


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=True)
