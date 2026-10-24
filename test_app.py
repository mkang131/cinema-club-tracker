"""Core acceptance checks for the course entry point, using isolated fixtures."""


def test_health_is_available_without_login(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_club_catalog_returns_saved_movie(client, make_movie, auth_headers):
    movie = make_movie()

    response = client.get("/api/v1/movies", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == movie.id
    assert response.json()["items"][0]["title"] == movie.title


def test_club_catalog_filters_by_year_parameter(client, make_movie, auth_headers):
    make_movie(title="The Matrix", year=1999)
    recent_movie = make_movie(title="Interstellar", year=2014)

    response = client.get(
        "/api/v1/movies", headers=auth_headers(), params={"year_from": 2010}
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [movie["id"] for movie in response.json()["items"]] == [recent_movie.id]


def test_unknown_movie_returns_not_found(client, auth_headers):
    response = client.get("/api/v1/movies/999", headers=auth_headers())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "movie_not_found"
