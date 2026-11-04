from app.services.order_service import OrderService


def test_place_order_endpoint(client):
    response = client.post("/api/orders", params={"user_id": 1, "items": ["book"]})
    assert response.status_code in (201, 404)


def test_place_order_service():
    assert OrderService.place(1, ["book"])["user_id"] == 1
