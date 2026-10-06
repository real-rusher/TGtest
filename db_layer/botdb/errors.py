class RepositoryError(Exception):
    """Basisklasse fuer alle Fehler der Datenbankschicht."""


class UserNotFound(RepositoryError):
    def __init__(self, user_id: int):
        super().__init__(f"User {user_id} existiert nicht (zuerst upsert_user aufrufen)")
        self.user_id = user_id


class ProductNotFound(RepositoryError):
    def __init__(self, product_id: str):
        super().__init__(f"Produkt '{product_id}' existiert nicht")
        self.product_id = product_id


class InvalidFact(RepositoryError):
    pass
