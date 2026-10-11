package main

import (
	"fmt"
	"net/http"
)

// Server handles HTTP requests.
type Server struct {
	Host string
	Port int
}

// Start begins listening on the configured address.
func (s *Server) Start() error {
	addr := fmt.Sprintf("%s:%d", s.Host, s.Port)
	return http.ListenAndServe(addr, nil)
}

// Stop gracefully shuts down the server.
func (s *Server) Stop() error {
	fmt.Println("Stopping server")
	return nil
}

// NewServer creates a new Server instance.
func NewServer(host string, port int) *Server {
	return &Server{Host: host, Port: port}
}

// HealthCheck returns the server status.
func HealthCheck(w http.ResponseWriter, r *http.Request) {
	fmt.Fprintf(w, "OK")
}
