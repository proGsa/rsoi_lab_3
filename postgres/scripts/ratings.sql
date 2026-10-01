CREATE TABLE rating
(
    id       SERIAL PRIMARY KEY,
    username VARCHAR(80) NOT NULL,
    stars    INT NOT NULL
        CHECK (stars BETWEEN 0 AND 100)
);

INSERT INTO rating (username, stars)
VALUES ('test', 5);

INSERT INTO rating (username, stars)
VALUES ('Test Max', 75);
GRANT USAGE ON SCHEMA public TO program;

GRANT SELECT, INSERT, UPDATE, DELETE
ON ALL TABLES IN SCHEMA public
TO program;

GRANT USAGE, SELECT
ON ALL SEQUENCES IN SCHEMA public
TO program;