-- Create the MariaDB/MySQL database and user for the "home_twin" integration (run once as root,
-- e.g. phpMyAdmin -> SQL, or: mariadb -u root -p < setup-db.sql).
-- Replace CHANGE_ME with your own password and use the same one in secrets.yaml.
CREATE DATABASE IF NOT EXISTS home_twin CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'home_twin'@'%' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON home_twin.* TO 'home_twin'@'%';
FLUSH PRIVILEGES;
