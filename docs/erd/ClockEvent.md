# ERD ClockEvent

```mermaid
erDiagram
    PRODUCT {
        int id PK
        string name
        string code
    }

    PRODUCT_CARD {
        int id PK
        int product_id FK
        string article
        string size
        string color
    }

    TECH_OPERATION {
        int id PK
        int product_card_id FK
        int operation_type_id FK
        int norm_seconds
    }

    OPERATION_TYPE {
        int id PK
        string name
    }

    PRODUCT ||--o{ PRODUCT_CARD : "has variants"
    PRODUCT_CARD ||--o{ TECH_OPERATION : "has operations"
    OPERATION_TYPE ||--o{ TECH_OPERATION : "defines type"