from backend.bot.signals import news


def test_empty_articles_returns_zero_sentiment():
    snap = news.score_articles([])
    assert snap.items == []
    assert snap.average_sentiment == 0.0


def test_positive_articles_have_positive_sentiment():
    articles = [
        {
            "title": "AAPL beats earnings and raises guidance, stock soars",
            "description": "Excellent results across all segments.",
            "url": "u",
            "publishedAt": "t",
        }
    ]
    snap = news.score_articles(articles)
    assert -1 <= snap.average_sentiment <= 1
    assert snap.average_sentiment > 0


def test_negative_articles_have_negative_sentiment():
    articles = [
        {
            "title": "AAPL crashes after disappointing miss, investors flee",
            "description": "Terrible quarter, guidance slashed.",
            "url": "u",
            "publishedAt": "t",
        }
    ]
    snap = news.score_articles(articles)
    assert snap.average_sentiment < 0


def test_fetch_news_uses_injected_client(fake_news_client):
    articles = news.fetch_news("AAPL", client=fake_news_client)
    assert len(articles) == 2
    fake_news_client.get_everything.assert_called_once()
