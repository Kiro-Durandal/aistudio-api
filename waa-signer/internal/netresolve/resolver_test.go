package netresolve

import (
	"context"
	"encoding/binary"
	"io"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestDNSAddress(t *testing.T) {
	for _, test := range []struct{ input, want string }{
		{"192.0.2.1", "192.0.2.1:53"},
		{"2001:db8::1", "[2001:db8::1]:53"},
		{"127.0.0.1:5353", "127.0.0.1:5353"},
		{"[::1]:5353", "[::1]:5353"},
		{"fe80::1%wlan0", "[fe80::1%wlan0]:53"},
		{"dns.example.com", ""},
		{"https://192.0.2.1/dns-query", ""},
		{"127.0.0.1:0", ""},
		{"127.0.0.1:99999", ""},
	} {
		t.Run(test.input, func(t *testing.T) {
			got, err := dnsAddress(test.input)
			if test.want == "" {
				if err == nil {
					t.Fatal("invalid DNS address was accepted")
				}
			} else if err != nil || got != test.want {
				t.Fatalf("dnsAddress = %q, %v; want %q", got, err, test.want)
			}
		})
	}
}

func TestConfigurePreservesResolverWithoutConfiguration(t *testing.T) {
	before := net.DefaultResolver
	for _, prefix := range []string{"", t.TempDir()} {
		source, err := Configure("", prefix)
		if err != nil || source != "" || net.DefaultResolver != before {
			t.Fatalf("unconfigured resolver changed: %q, %v", source, err)
		}
	}
}

func TestInvalidConfigurationDoesNotReplaceResolver(t *testing.T) {
	before := net.DefaultResolver
	prefix := writeResolvConf(t, "nameserver invalid.example\n")
	for _, test := range []struct{ server, prefix string }{
		{"dns.example.com", prefix},
		{"", prefix},
	} {
		if _, err := Configure(test.server, test.prefix); err == nil {
			t.Fatal("invalid DNS configuration was accepted")
		}
		if net.DefaultResolver != before {
			t.Fatal("invalid configuration replaced the resolver")
		}
	}
}

func TestConfiguredResolverLookup(t *testing.T) {
	for _, test := range []struct {
		name, configuration string
		truncateUDP         bool
	}{
		{"termux-resolv-conf", "file", false},
		{"explicit-overrides-file", "explicit", false},
		{"tcp-fallback", "file", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			preserveResolver(t)
			address, tcpQueries := startDNSServer(t, test.truncateUDP)
			prefix := writeResolvConf(t, "# custom Termux settings\nsearch example.invalid\nnameserver invalid.example\nnameserver "+address+" # local fixture\n")
			server := ""
			if test.configuration == "explicit" {
				prefix = writeResolvConf(t, "nameserver 127.0.0.1:1\n")
				server = address
			}
			if _, err := Configure(server, prefix); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			addresses, err := net.DefaultResolver.LookupNetIP(ctx, "ip4", "signer-dns-test.invalid.")
			if err != nil || len(addresses) != 1 || addresses[0] != netip.MustParseAddr("203.0.113.7") {
				t.Fatalf("local DNS lookup failed: %v, %v", addresses, err)
			}
			if test.truncateUDP && tcpQueries.Load() == 0 {
				t.Fatal("truncated UDP response did not fall back to TCP")
			}
		})
	}
}

func TestResolverDialHonorsCancellation(t *testing.T) {
	preserveResolver(t)
	if _, err := Configure("127.0.0.1:53", ""); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if connection, err := net.DefaultResolver.Dial(ctx, "udp", "192.0.2.1:53"); err == nil {
		connection.Close()
		t.Fatal("cancelled DNS dial succeeded")
	}
}

func preserveResolver(t *testing.T) {
	t.Helper()
	before := net.DefaultResolver
	t.Cleanup(func() { net.DefaultResolver = before })
}

func writeResolvConf(t *testing.T, content string) string {
	t.Helper()
	prefix := t.TempDir()
	directory := filepath.Join(prefix, "etc")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, "resolv.conf"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return prefix
}

// startDNSServer serves a fixed documentation-only IP on loopback. Tests never
// contact public DNS or send Google credentials.
func startDNSServer(t *testing.T, truncateUDP bool) (string, *atomic.Uint32) {
	t.Helper()
	tcp, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { tcp.Close() })
	udp, err := net.ListenPacket("udp4", tcp.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { udp.Close() })
	tcpQueries := new(atomic.Uint32)
	go func() {
		buffer := make([]byte, 4096)
		for {
			n, peer, err := udp.ReadFrom(buffer)
			if err != nil {
				return
			}
			udp.WriteTo(dnsResponse(buffer[:n], truncateUDP), peer)
		}
	}()
	go func() {
		for {
			connection, err := tcp.Accept()
			if err != nil {
				return
			}
			func() {
				defer connection.Close()
				connection.SetDeadline(time.Now().Add(3 * time.Second))
				var size [2]byte
				if _, err := io.ReadFull(connection, size[:]); err != nil {
					return
				}
				query := make([]byte, binary.BigEndian.Uint16(size[:]))
				if _, err := io.ReadFull(connection, query); err != nil {
					return
				}
				tcpQueries.Add(1)
				response := dnsResponse(query, false)
				binary.BigEndian.PutUint16(size[:], uint16(len(response)))
				connection.Write(append(size[:], response...))
			}()
		}
	}()
	return tcp.Addr().String(), tcpQueries
}

func dnsResponse(query []byte, truncated bool) []byte {
	end := 12
	for end < len(query) && query[end] != 0 {
		end += int(query[end]) + 1
	}
	end += 5 // terminating zero, QTYPE and QCLASS
	if end > len(query) {
		return nil
	}
	response := append([]byte(nil), query[:end]...)
	response[2], response[3] = 0x81, 0x80
	for index := 4; index < 12; index++ {
		response[index] = 0
	}
	binary.BigEndian.PutUint16(response[4:6], 1)
	if truncated {
		response[2] |= 0x02
		return response
	}
	binary.BigEndian.PutUint16(response[6:8], 1)
	return append(response, []byte{0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4, 203, 0, 113, 7}...)
}

func TestConfiguredServersAreUsedInOrder(t *testing.T) {
	preserveResolver(t)
	addresses := []string{}
	for range 2 {
		address, _ := startDNSServer(t, false)
		addresses = append(addresses, address)
	}
	prefix := writeResolvConf(t, "nameserver "+strings.Join(addresses, "\nnameserver ")+"\n")
	if _, err := Configure("", prefix); err != nil {
		t.Fatal(err)
	}
	for _, expected := range addresses {
		connection, err := net.DefaultResolver.Dial(context.Background(), "udp", "[::1]:53")
		if err != nil {
			t.Fatal(err)
		}
		actual := connection.RemoteAddr().String()
		connection.Close()
		if actual != expected {
			t.Fatalf("DNS dial went to %q, want %q", actual, expected)
		}
	}
}
