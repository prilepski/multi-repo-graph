package org.demo;

public interface OrderRepository {
    void save(String id);
    String find(String id);
}
