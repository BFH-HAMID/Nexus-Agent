"""Fixed HTTPS destinations. Configuration enables IDs, never arbitrary fetch URLs."""
SOURCES = {
    'bbc': ('World', 'BBC News · World', 'https://feeds.bbci.co.uk/news/world/rss.xml', 'https://www.bbc.co.uk/usingthebbc/terms'),
    'aljazeera': ('World', 'Al Jazeera', 'https://www.aljazeera.com/xml/rss/all.xml', 'https://www.aljazeera.com/terms-and-conditions'),
    'dw': ('World', 'DW', 'https://rss.dw.com/rdf/rss-en-world', 'https://www.dw.com/en/terms-of-use/a-16578410'),
    'guardian': ('World', 'The Guardian', 'https://www.theguardian.com/world/rss', 'https://www.theguardian.com/help/terms-of-service'),
    'npr': ('World', 'NPR', 'https://feeds.npr.org/1004/rss.xml', 'https://www.npr.org/about-npr/179876898/terms-of-use'),
    'bbc-bangla': ('Bangladesh', 'BBC Bangla', 'https://feeds.bbci.co.uk/bengali/rss.xml', 'https://www.bbc.co.uk/usingthebbc/terms'),
    'prothom-alo': ('Bangladesh', 'Prothom Alo', 'https://www.prothomalo.com/feed', 'https://www.prothomalo.com/terms-of-use'),
    'coindesk': ('Crypto', 'CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/', 'https://www.coindesk.com/terms'),
    'cointelegraph': ('Crypto', 'Cointelegraph', 'https://cointelegraph.com/rss', 'https://cointelegraph.com/terms-and-privacy'),
    'decrypt': ('Crypto', 'Decrypt', 'https://decrypt.co/feed', 'https://decrypt.co/terms-of-service'),
    'hacker-news': ('Security', 'The Hacker News', 'https://feeds.feedburner.com/TheHackersNews', 'https://thehackernews.com/p/terms-and-conditions.html'),
    'bleeping': ('Security', 'BleepingComputer', 'https://www.bleepingcomputer.com/feed/', 'https://www.bleepingcomputer.com/terms-of-use/'),
    'krebs': ('Security', 'Krebs on Security', 'https://krebsonsecurity.com/feed/', 'https://krebsonsecurity.com/about/'),
}
QUAKES = 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson'
WEATHER = 'https://api.open-meteo.com/v1/forecast'
GEOCODE = 'https://geocoding-api.open-meteo.com/v1/search'
CRYPTO = 'https://api.coingecko.com/api/v3/coins/markets'
BINANCE = 'https://api.binance.com/api/v3/ticker/24hr'
