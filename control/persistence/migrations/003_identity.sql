CREATE TABLE login_limits(email text PRIMARY KEY,window_start timestamptz NOT NULL,attempts integer NOT NULL);
