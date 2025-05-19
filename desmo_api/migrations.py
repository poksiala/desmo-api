MIGRATIONS = [
    [
        """
        CREATE TABLE meta (
            version integer PRIMARY KEY
        );
        """,
        "INSERT INTO meta (version) VALUES (0)",
    ],
    [
        """
        CREATE TABLE jail (
            name text PRIMARY KEY,
            host text NOT NULL,
            ip text NOT NULL UNIQUE,
            state text NOT NULL
        );
        """,
        """
        CREATE TABLE jail_package (
            jail_name text NOT NULL REFERENCES jail(name) ON DELETE CASCADE,
            name text NOT NULL
        );
        """,
        """
        CREATE TABLE jail_command (
            jail_name text NOT NULL REFERENCES jail(name) ON DELETE CASCADE,
            command text NOT NULL,
            order_no integer NOT NULL
        );
        """,
    ],
    [
        """
        CREATE TABLE prison (
            name text PRIMARY KEY,
            base text NOT NULL,
            replicas integer NOT NULL
        );
        """,
        """
        CREATE TABLE prison_package (
            prison_name text NOT NULL REFERENCES prison(name) ON DELETE CASCADE,
            name text NOT NULL
        );
        """,
        """
        CREATE TABLE prison_command (
            prison_name text NOT NULL REFERENCES prison(name) ON DELETE CASCADE,
            command text NOT NULL,
            order_no integer NOT NULL
        );
        """,
        """
        ALTER TABLE jail ADD COLUMN base text NOT NULL DEFAULT '14.0-RELEASE-base';
        """,
        """
        ALTER TABLE jail ADD COLUMN prison_name text REFERENCES prison(name) ON DELETE CASCADE DEFAULT NULL;
        """,
    ],
    [
        """CREATE table namespace (name text PRIMARY KEY);""",
        """INSERT INTO namespace (name) VALUES ('default') ON CONFLICT DO NOTHING;""",
        """ALTER TABLE jail ADD COLUMN namespace text REFERENCES namespace(name) ON DELETE RESTRICT DEFAULT 'default';""",
        """ALTER TABLE prison ADD COLUMN namespace text REFERENCES namespace(name) ON DELETE RESTRICT DEFAULT 'default';""",
    ],
    [
        """DROP TABLE prison_command;""",
        """DROP TABLE prison_package;""",
        """DROP TABLE jail_command;""",
        """DROP TABLE jail_package;""",
    ],
    [
        """
        CREATE TABLE image (
            digest TEXT PRIMARY KEY,
            data BYTEA,
            desmofile TEXT
        );
        """,
        """
        ALTER TABLE jail ADD COLUMN image_digest text DEFAULT NULL;
        """,
        """
        ALTER TABLE prison ADD COLUMN image_digest text DEFAULT NULL;
        """,
    ],
]
