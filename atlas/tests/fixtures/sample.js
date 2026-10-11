import express from "express";
const { Router } = require("express");

class UserService {
    constructor(db) {
        this.db = db;
    }
    async findUser(id) {
        return this.db.query(id);
    }
    async deleteUser(id) {
        return this.db.remove(id);
    }
}

function createApp(config) {
    const app = express();
    const router = Router();
    return app;
}

const formatResponse = (data) => JSON.stringify(data);
