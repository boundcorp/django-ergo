def test_web_app_routes(client, settings, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<div id=root></div>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    settings.FRONTEND_DIST = str(tmp_path)

    assert b"id=root" in client.get("/s/123").content
    assert client.get("/assets/app.js")["Content-Type"] == "text/javascript"
    assert client.get("/assets/missing.js").status_code == 404

    settings.FRONTEND_DIST = str(tmp_path / "nope")
    assert client.get("/").status_code == 503
