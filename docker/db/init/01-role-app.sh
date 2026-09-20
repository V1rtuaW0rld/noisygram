#!/bin/bash
# Rôle applicatif, créé UNE FOIS, à l'initialisation du volume PostgreSQL.
#
# ⚠️ Ce script ne tourne QUE si le dossier de données est vide — c'est-à-dire à
# la toute première initialisation. Sur un volume existant, il est ignoré sans
# le dire. C'est voulu : c'est le seul moment où l'on dispose du
# superutilisateur ET d'aucune donnée à préserver.
#
# Pourquoi un rôle dédié plutôt que POSTGRES_USER
# L'image postgres crée POSTGRES_USER en SUPERUTILISATEUR, et un
# superutilisateur contourne la Row-Level Security quoi qu'on fasse — `FORCE
# ROW LEVEL SECURITY` ne s'applique qu'au propriétaire des tables, pas
# au-dessus. Sans ce rôle, la politique posée par les migrations 10 et 11
# existerait sans rien protéger, et la séparation par projet serait une
# illusion.
#
# Le rôle applicatif n'est ni superutilisateur ni BYPASSRLS. Il lui faut :
#   · CONNECT sur la base ;
#   · USAGE et CREATE sur le schéma, pour que les migrations puissent créer
#     les tables. Il en devient propriétaire, donc il pourra aussi les ALTER.
#
# Le rôle `POSTGRES_USER` reste, lui, pour la maintenance : psql, pg_dump,
# restauration.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE noisygram_app LOGIN PASSWORD '$POSTGRES_PASSWORD';
    ALTER ROLE noisygram_app NOSUPERUSER NOBYPASSRLS;
    GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO noisygram_app;
    GRANT USAGE, CREATE ON SCHEMA public TO noisygram_app;
EOSQL

echo "rôle applicatif noisygram_app créé (ni superutilisateur, ni BYPASSRLS)"
