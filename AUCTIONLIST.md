# Auction List Design Doc (Suggested Ordering via AI Ranker)

Based on our meeting, the technical design is as follows:

We want to use an auction ranker service provided by the internal AI team to personalize auction ordering for dealers. The ranker will first build and refresh auction embeddings from our auction list data. Then, when the front-end asks for the suggested ordering, our API will call the AI ranker and apply the returned auction order.

## Goal

Use the internal AI team's ranker service to personalize the auction list when the front-end sends:

`GET /api/v1/auctions/?ordering=suggested`

Default behavior stays unchanged: auctions are ordered by `end_time, id`.

## Auction Feed For Ranker (Current / Temporary)

For now, the ranker would poll the existing dealer auction list every 10 seconds:

`GET /api/v1/auctions/?ordering=end_time`

- **Authorization**: `Token <dealer-token>`
- **Implementation**: `apps.auctions.api.AuctionViewSet.list`
- **Notes**
  - This route has been used by Front-end.
  - It returns active, non-manual, unfinished auctions with `end_time` in the future.
  - Unlike what we thought, the list already is unpaginated.

### Getting the current dealer token (deprecated)

To get the current dealer token (this endpoint is deprecated and would not be used later):

`POST /api/v1/user/login/`

- **Content-Type**: `application/json`
- **Body**

```json
{
  "username": "<dealer-username>",
  "password": "<dealer-password>"
}
```

Then use the returned token:

`Authorization: Token <token>`

## Future Internal Feed (Recommended)

Later, we should expose the same auction list response under an internal route to avoid implicit coupling, for example:

`GET /api/internal/auctions/`

- **Authorization**: `Token <api-key>`
- **Pattern**: Use the existing service-token pattern:
  - Header: `Authorization: Token <api-key>`
- **Response shape**: The internal feed should keep the current response shape.
- **Dealer-specific fields**: Dealer-specific fields should use neutral values because an API-key service user has no dealer object:
  - `dealer_suite_car = false`
  - `bid.user_latest = null`

## Suggested Ordering (Backend Behavior)

When the front-end sends `ordering=suggested`, the backend calls the AI ranker:

`POST <ranker-base-url>/sort-auctions`

- **Content-Type**: `application/json`
- **Body**

```json
{
  "dealer_id": 123
}
```

### Expected response

```json
{
  "auction_ids": [12, 10, 11]
}
```

### Backend steps

- Build the normal filtered auction queryset.
- Call the ranker with `dealer_id = request.user.id`.
- Use a **50 ms timeout**.
- Order ranked auctions by returned `auction_ids`.
- Keep unranked auctions after ranked auctions using fallback order: `end_time, id`.
- On timeout, connection error, non-2xx, invalid JSON, or invalid ids, fall back to `end_time, id`.

## Open Decisions

- Final internal feed path.
- Ranker base URL.

## Owner / Contact

Regards,  
Danial Keimasi

