--
-- PostgreSQL database dump
--

-- Dumped from database version 15.8
-- Dumped by pg_dump version 15.8

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: bursar; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS bursar;


--
-- Name: SCHEMA bursar; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA bursar IS 'Backend-only Bursar accounting, catalog, and billing schema.';


-- Bursar owns its timestamp trigger helper.  This keeps the backend schema
-- independent of Supabase's public helper while retaining identical behavior.
CREATE OR REPLACE FUNCTION bursar.handle_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


--
-- Name: credit_tx_type; Type: TYPE; Schema: bursar; Owner: -
--

CREATE TYPE bursar.credit_tx_type AS ENUM (
    'purchase',
    'subscription',
    'signup_bonus',
    'usage',
    'refund',
    'adjustment',
    'team_usage',
    'cycle_grant',
    'cycle_grant_revoke'
);


--
