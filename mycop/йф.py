    import osmnx as ox

city = "Astana, Kazakhstan"

# 1. Скачать ВСЕ здания города с адресами и геометрией
print("Загружаем здания...")
buildings = ox.features_from_place(city, tags={'building': True})
print(f"Загружено зданий: {len(buildings)}")

# 2. Скачать ВСЕ школы, детсады и супермаркеты
print("Загружаем инфраструктуру...")
amenities = ox.features_from_place(
    city,
    tags={'amenity': ['school', 'kindergarten', 'supermarket']}
)
print(f"Загружено объектов инфраструктуры: {len(amenities)}")

# 3. Посмотреть пример полученных данных
print(buildings[['geometry', 'addr:street', 'addr:housenumber']].head())