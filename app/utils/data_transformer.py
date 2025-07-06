class DataTransformer:
    def normalize_prices(self, data):
        for item in data:
            if isinstance(item.get('price'), str):
                item['price'] = float(item['price'])
        return data
